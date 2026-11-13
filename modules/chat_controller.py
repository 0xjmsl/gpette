import json
import datetime
import time
from modules.thread_controller import ThreadController
from modules.function_registry import FunctionRegistry
from models.claudeCodeService import ClaudeCodeService
from utils.message_logger import log_message_flow, truncate_message
import os
from modules.prompts import default_system_prompt
from modules.context_compactor import compact as compact_context
from modules.thread_joins import get_group
from config import Config
from models.conversation_db import ConversationDB
from modules import sender_names
from memory.manager import MemoryManager
from modules.profiles import resolve_profile, profile_for_conversation

# A timer fire that answers exactly this sends nothing back (see _handle_proactive_message).
NO_REPLY_TOKEN = "[NO_REPLY]"


class ChatController:
    def __init__(self, service="lmstudio", thread_controller: ThreadController = None, function_registry: FunctionRegistry = None):
        self.thread_controller = thread_controller
        self.function_registry = function_registry
        self.service = self._create_service(service)
        self._profile_services = {}    # (provider, model) -> service, for profiles that name one
        self._conversation_db = ConversationDB()
        self._memory = MemoryManager() if Config.MEMORY_ENABLED else None

    def _service_for(self, profile):
        """The LLM service a profile runs on. No `provider` = the active service
        (the one /api/config can switch). A named provider gets its own instance,
        built on first use and kept; `model` is set on it for API providers.
        Claude Code is one instance only (it owns claude_sessions.json); its
        model, like the rest of its environment, rides on the profile."""
        provider = profile.provider if profile else None
        if not provider:
            return self.service
        if provider == "claude-code":
            if isinstance(self.service, ClaudeCodeService):
                return self.service
            key = ("claude-code", "")
        else:
            key = (provider, profile.model or "")
        svc = self._profile_services.get(key)
        if svc is None:
            svc = self._create_service(provider)
            if profile.model and provider != "claude-code" and hasattr(svc, "model"):
                svc.model = profile.model
            self._profile_services[key] = svc
        return svc

    def _tools_for(self, profile):
        """Tool declarations (OpenAI format) this profile may call."""
        tools = self.function_registry.get_function_declarations_for_tools()
        if profile is not None and profile.tools is not None:
            tools = [t for t in tools if profile.tool_allowed(t.get("function", {}).get("name"))]
        return tools

    def _create_service(self, service):
        # Provider SDKs are imported on first use, so only the ones you use need installing.
        if service == "gemini":
            from models.geminiService import GeminiService
            return GeminiService()
        elif service == "ollama":
            from models.ollamaService import OllamaService
            return OllamaService()
        elif service == "lmstudio":
            from models.lmStudioService import LMStudioService
            return LMStudioService()
        elif service == "claude":
            from models.claudeService import ClaudeService
            return ClaudeService()
        elif service == "openai":
            from models.openAIService import OpenAIService
            return OpenAIService()
        elif service == "claude-code":
            return ClaudeCodeService()

        raise ValueError(f"Unknown LLM service: '{service}'. Valid options: gemini, ollama, lmstudio, claude, openai, claude-code")

    def get_chat_history(self, mode=None):
        return self.thread_controller.get_chat_history(mode)

    def get_system_prompt(self, profile=None) -> str:
        if profile is not None and profile.system_prompt:
            return f"{profile.system_prompt}\n"
        base_prompt = default_system_prompt()  # fresh each call: it carries the current time
        return f"{base_prompt}\n"

    def _resolve_sender_name(self, inbox_id):
        """Resolve a sender's display name, using DB cache first, then live resolution."""
        if not inbox_id:
            return None, None

        # Check DB cache
        cached = self._conversation_db.get_resolved_contact(inbox_id)
        if cached:
            return cached.get('display_name'), cached.get('resolution_source')

        # Get eth address from DB if available
        eth_address = self._conversation_db.get_participant_address(inbox_id)

        # Live resolution (a plug-in, or a short inbox id). Only real names are cached.
        name, source = sender_names.resolve(inbox_id, address=eth_address)
        if source != 'fallback':
            self._conversation_db.store_resolved_contact(inbox_id, eth_address, name, source)
        return name, source

    def _prepare_messages_with_sender_context(self, message, conversation_history, system_prompt, conversation_id, is_group):
        """
        Enrich messages with sender identity for the LLM.
        - Group chats: prefix each user message with 'sendername: '
        - 1:1 chats: add resolved name to system prompt
        """
        enhanced_prompt = system_prompt

        if is_group:
            # Collect unique participant names for system prompt context
            participant_names = set()

            # Prefix history messages
            for msg in conversation_history:
                if msg.get('role') == 'user' and msg.get('senderInboxId'):
                    sender_name, _ = self._resolve_sender_name(msg['senderInboxId'])
                    if sender_name:
                        participant_names.add(sender_name)
                        content = msg.get('content')
                        if isinstance(content, dict) and content.get('type') == 'text':
                            content['value'] = f"{sender_name}: {content['value']}"

            # Prefix current message
            sender_inbox = message.get('senderInboxId')
            if sender_inbox:
                sender_name, _ = self._resolve_sender_name(sender_inbox)
                if sender_name:
                    participant_names.add(sender_name)
                    content = message.get('content')
                    if isinstance(content, dict) and content.get('type') == 'text':
                        content['value'] = f"{sender_name}: {content['value']}"

            if participant_names:
                names_list = ', '.join(sorted(participant_names))
                enhanced_prompt += (
                    f"\n---\nGROUP CONVERSATION\n"
                    f"Participants: {names_list}\n"
                    f"Each user message is prefixed with the sender's name followed by a colon. "
                    f"Address users by their name when responding.\n---\n"
                )
        else:
            # 1:1 conversation - resolve the user's name for system prompt
            sender_inbox = message.get('senderInboxId')
            if sender_inbox:
                sender_name, source = self._resolve_sender_name(sender_inbox)
                if sender_name and source != 'fallback':
                    message['senderName'] = sender_name     # for Claude Code (no system prompt)
                    enhanced_prompt += (
                        f"\n---\nThe user's resolved name is: {sender_name} (source: {source}). "
                        f"If the user asks to be called something else, use that name instead.\n---\n"
                    )

        return enhanced_prompt

    # def get_functions_list(self, mode) -> str:
    #     try:
    #         functions_with_descriptions = self.function_registry.get_functions_with_descriptions(mode)
    #         parsed_functions = json.loads(functions_with_descriptions)
    #         return "\n".join(
    #             [f"- {func.get('functionName', 'Unknown')}: {func.get('description', 'No description')}"
    #              for func in parsed_functions]
    #         )
    #     except json.JSONDecodeError:
    #         return "Error: Unable to parse function descriptions. Advice to tell Admin to check functions"
    #     except Exception as e:
    #         return f"Error: Unable to parse function descriptions. {e}"
        
    def receive_Message_ChatController(self, message: dict):
        conversation_id = message.get("conversationId")

        # Check for Proactive trigger
        if message.get("role") == "Proactive":
            return self._handle_proactive_message(message)

        try:

            sender_id = message.get('senderInboxId', 'unknown')
            content_preview = truncate_message(message.get('content'))
            log_message_flow("CLIENT", "ChatController", "user_message", sender_id, content_preview)
            # Classification rides on the envelope (set by xmtp_daemon, carried
            # through standardize_message) — no DB lookup on the hot path.
            is_group = message.get("kind") == "group"
            # Which environment answers: a human message is matched by group id,
            # then sender; a tool result (no sender) reuses its conversation's profile.
            if message.get("role") == "user" and message.get("senderInboxId"):
                profile = resolve_profile(conversation_id, message.get("senderInboxId"), message.get("kind"))
            else:
                profile = profile_for_conversation(conversation_id, message.get("kind"), self._conversation_db)
            service = self._service_for(profile)
            conversation_history = self.thread_controller.get_chat_history(
                conversation_id, limit=Config.COMPACTION_AFTER + 20)
            # Exclude the last message (the current user input) from history —
            # it will be passed separately to send_message()
            conversation_history = conversation_history[:-1] if conversation_history else []

            # The saved summary always applies; a fold (a model call) never runs mid tool loop.
            conversation_history, context_note = compact_context(
                conversation_history, service, conversation_id, self._conversation_db,
                fold=message.get("role") != "tool", profile=profile, incoming=message)
            system_prompt = self.get_system_prompt(profile)
            if context_note:
                system_prompt = system_prompt + context_note
                message['contextNote'] = context_note   # for Claude Code (no system prompt)

            # Enrich with sender identity (group prefixes or 1:1 name in system prompt)
            system_prompt = self._prepare_messages_with_sender_context(
                message, conversation_history, system_prompt, conversation_id, is_group
            )

            # Memory search — inject relevant past context
            if self._memory and message.get("role") != "tool":
                query = message.get("content")
                if isinstance(query, dict):
                    query = query.get("value", "")
                memory_context = self._memory.search(query, conversation_id)
                if memory_context:
                    system_prompt += memory_context

            # Get function declarations in OpenAI tools format (the profile's subset)
            tools = self._tools_for(profile)

            # Send message to service with tools declarations
            service_response = service.send_message(
                message,
                conversation_history,
                system_prompt,
                json.dumps(tools) if tools else None,
                is_group=is_group,
                profile=profile,
            )
            now = datetime.datetime.now()
            timestamp = int(now.timestamp() * 1000)
            final_response = {
                "role": "assistant",
                'address': self.thread_controller.get_address(),
                "content": service_response.get("content"),
                "timestamp": timestamp,
                "conversationId": service_response.get("conversationId"),
                "tool_calls": service_response.get("tool_calls"),
                "token_count": service_response.get("token_count"),
                "source": "ChatController"
            }

            # Memory indexing — store this interaction for future recall
            if self._memory:
                self._memory.index_conversation(conversation_id, conversation_history + [message, final_response])

            return self.thread_controller.add_message(final_response)
        except Exception as e:
            log_message_flow("ChatController", "SYSTEM", "error", None, str(e))
            return self.thread_controller.add_message(self.system_message("error", "Error sending message 😤"))

    def system_message(self, type: str, msg: str):
        current_time = int(time.time())
        return {
            "role": "system",
            'address': self.thread_controller.get_address(),
            "timestamp": current_time,
            "content": {"type": type, "value": msg},
            "tool_calls": None,
            "source": "ChatController"
        }

    def get_current_service(self):
        return self.service.__class__.__name__

    def change_service(self, new_service: str):
        try:
            del self.service
            self.service = self._create_service(new_service)
            return self.system_message("message", f"Service changed to {new_service}")
        except Exception as e:
            log_message_flow("ChatController", "SYSTEM", "service_change_error", None, str(e))
            return self.system_message("error", f"Error changing service to {new_service}")

    def _handle_proactive_message(self, message: dict):
        """Handle proactive system trigger (model-agnostic).

        Builds a tool-call + tool-return pair, stores to DB, then sends to
        whichever model is active. Each model's send_message() handles its
        own format conversion internally.
        """
        conversation_id = message.get("conversationId")
        proactive_context = message.get("proactive_context", {})

        try:
            timer_data = proactive_context.get('timer')
            if not timer_data:
                log_message_flow("ChatController", "SYSTEM", "warning", conversation_id, "Proactive message without timer data, ignoring")
                return None

            skip_session = bool(timer_data.get('skip_session', False))
            tool_name = "timer_fired"
            log_message_flow("ProactiveSystem", "ChatController", "timer_trigger", conversation_id,
                             f"timer={timer_data.get('timer_id')} skip_session={skip_session}")

            context_text = self._format_timer_context(timer_data)

            now = datetime.datetime.now()
            ts_ms = int(now.timestamp() * 1000)
            call_id = f"call_proactive_{int(now.timestamp())}"

            # 1. Fake assistant message that "made" the tool call
            fake_assistant = {
                "role": "assistant",
                "content": {"type": "text", "value": ""},
                "timestamp": ts_ms,
                "conversationId": conversation_id,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": "{}"}}],
                "source": "ProactiveSystem"
            }

            # 2. Tool return carrying the context/instructions
            tool_return = {
                "role": "tool",
                "content": context_text,
                "tool_call_id": call_id,
                "timestamp": ts_ms + 1,
                "conversationId": conversation_id,
                "source": "ProactiveSystem"
            }

            # Store both directly (bypass ThreadController to avoid SocketIO dispatch / FunctionRegistry execution)
            self._conversation_db.store_message(fake_assistant)
            self._conversation_db.store_message(tool_return)

            # Timer fires have no envelope, so kind comes from the DB here — the
            # conversation row is guaranteed committed by an earlier real message.
            is_group = self._conversation_db.is_group_conversation(conversation_id)
            profile = profile_for_conversation(
                conversation_id, "group" if is_group else "dm", self._conversation_db)
            service = self._service_for(profile)

            # Build system prompt
            system_prompt = self.get_system_prompt(profile)

            # Get conversation history (skip if skip_session)
            if skip_session:
                conversation_history = []
            else:
                conversation_history = self.thread_controller.get_chat_history(
                    conversation_id, limit=Config.COMPACTION_AFTER + 20)
                conversation_history = conversation_history[:-1] if conversation_history else []
                conversation_history, context_note = compact_context(
                    conversation_history, service, conversation_id, self._conversation_db,
                    profile=profile, incoming=tool_return)
                if context_note:
                    system_prompt = system_prompt + context_note
                    tool_return['contextNote'] = context_note   # for Claude Code (no system prompt)

            # Get function declarations so the LLM can use tools in its response
            tools = self._tools_for(profile)

            # Call LLM — each model handles skip_session internally
            service_response = service.send_message(
                tool_return,
                conversation_history,
                system_prompt,
                json.dumps(tools) if tools else None,
                skip_session=skip_session,
                is_group=is_group,
                profile=profile,
            )

            # A timer fire may decide there is nothing worth saying (quiet hours,
            # silent upkeep). Replying with exactly NO_REPLY_TOKEN ends the run
            # without sending anything. User messages never take this path.
            if self._is_no_reply(service_response.get("content")):
                log_message_flow("ChatController", "SYSTEM", "timer_no_reply", conversation_id,
                                 f"timer={timer_data.get('timer_id')} answered {NO_REPLY_TOKEN}, nothing sent")
                return None

            ts_response = int(datetime.datetime.now().timestamp() * 1000)
            final_response = {
                "role": "assistant",
                "address": self.thread_controller.get_address(),
                "content": service_response.get("content"),
                "timestamp": ts_response,
                "conversationId": conversation_id,
                "tool_calls": service_response.get("tool_calls"),
                "token_count": service_response.get("token_count"),
                "source": "ChatController"
            }

            # Thread joins: broadcast to all conversations in the group
            group = get_group(conversation_id)
            if len(group) > 1:
                for conv_id in group:
                    msg = dict(final_response)
                    msg["conversationId"] = conv_id
                    self.thread_controller.add_message(msg)
                return

            return self.thread_controller.add_message(final_response)

        except Exception as e:
            log_message_flow("ChatController", "SYSTEM", "proactive_error", None, str(e))
            return None

    @staticmethod
    def _is_no_reply(content) -> bool:
        """True when a timer-fire response is only the no-reply token."""
        text = content.get("value") if isinstance(content, dict) else content
        if not isinstance(text, str):
            return False
        return text.strip().strip("`*\"' .").upper() in (NO_REPLY_TOKEN, NO_REPLY_TOKEN.strip("[]"))

    def _format_timer_context(self, timer: dict) -> str:
        """Format a timer's data into prompt text for the AI"""
        description = timer.get('description', 'Timer')
        instructions = timer.get('instructions', '')
        timer_type = timer.get('timer_type', 'one_shot')

        recurring_note = " (Recurring timer, will fire again automatically.)" if timer_type == 'recurring' else ""

        return f"Timer fired: {description}. Instructions: {instructions}{recurring_note}"