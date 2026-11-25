import json
import datetime
import time
from modules.thread_controller import ThreadController
from modules.function_registry import FunctionRegistry
from models.claudeCodeService import ClaudeCodeService
from utils.message_logger import log_message_flow, truncate_message
import os
from modules.prompts import default_system_prompt, blocks_note
from modules.context_compactor import compact as compact_context
from modules.thread_joins import get_group
from config import Config
from models.conversation_db import ConversationDB
from modules import sender_names
from memory.manager import MemoryManager
from modules.profiles import resolve_profile, profile_for_conversation, profile_for_timer, register_outreach
from modules import group_pace, send_block, admin_turn, a2ui_block, msg_tags, reply_block

# An answer that is exactly this sends nothing back, whatever started the turn.
NO_REPLY_TOKEN = "[NO_REPLY]"


def is_no_reply_text(text) -> bool:
    """The no-reply token alone, or on its own last non-empty line."""
    if not isinstance(text, str):
        return False
    tokens = (NO_REPLY_TOKEN, NO_REPLY_TOKEN.strip("[]"))
    if text.strip().strip("`*\"' .").upper() in tokens:
        return True
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return bool(lines) and lines[-1].strip().strip("`*\"' .").upper() in tokens


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
        # API engines. (Claude Code ignores this prompt; it gets the same blocks
        # section appended in claudeCodeService._run_claude.)
        blocks = blocks_note(profile, claude_code=False) if profile is not None else ""
        if profile is not None and profile.system_prompt:
            return f"{profile.system_prompt}\n\n{blocks}\n"
        base_prompt = default_system_prompt()  # fresh each call: it carries the current time
        return f"{base_prompt}\n{blocks}\n" if blocks else f"{base_prompt}\n"

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

            # Prefix current message (a stacked batch carries its own per-line names)
            sender_inbox = message.get('senderInboxId')
            if sender_inbox and not message.get('prefixed'):
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
            # [ADMIN_TURN] wake-ups are only for the profile they were meant for, and
            # only while it may still be woken (the config can change under a running gpette).
            if not self._wake_ups_ok(message, profile):
                log_message_flow("ChatController", "SYSTEM", "admin_turn_dropped", conversation_id,
                                 f"a wake-up reached profile {profile.name}, which it was not meant for: dropped")
                return None
            service = self._service_for(profile)
            conversation_history = self.thread_controller.get_chat_history(
                conversation_id, limit=Config.COMPACTION_AFTER + 20)
            # Exclude the current user input from history — it is passed separately
            # to send_message(). A stacked batch (thread_controller) is N stored messages.
            batch = message.get("batch") or []
            drop = max(len(batch), 1)
            conversation_history = conversation_history[:-drop] if conversation_history else []
            if batch:
                self._merge_batch(message, batch, is_group)
            if is_group and message.get("role") == "user":
                self._append_text(message, group_pace.note(conversation_id))

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
            # [SEND_XMTP_MESSAGE]: the message is sent and the turn comes back with
            # the result; this answer is not delivered, the turn is not over.
            if self._caught_send_block(service_response, profile, service, conversation_id, message.get("kind")):
                return None
            # [ADMIN_TURN]: the text before it is the answer; the main gets a wake-up.
            if self._caught_admin_turn(service_response, profile, service, conversation_id, message.get("kind")):
                return None
            # <a2ui>: an interface the person's app draws, sent once it passes the check.
            if self._caught_a2ui(service_response, profile, service, conversation_id, message.get("kind")):
                return None
            # [NO_REPLY] sends nothing, whatever started the turn.
            if not is_group and self._is_no_reply(service_response.get("content")):
                log_message_flow("ChatController", "SYSTEM", "no_reply", conversation_id,
                                 f"answered {NO_REPLY_TOKEN}, nothing sent")
                return None
            # A group answer: the same, and a bot that posted there less than
            # group_reply_secs ago is held back (its tools already ran).
            if is_group and not service_response.get("tool_calls") and                     not self._group_may_send(conversation_id, service_response.get("content"), "group_turn"):
                return None
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
            # [REPLY]: the answer goes out as a reply to one tagged message.
            self._apply_reply_block(final_response, conversation_id)

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

            # A fire runs as what the timer says: its profile, its inbox, its
            # conversation. Timer fires have no envelope, so kind comes from the DB
            # (the conversation row was committed by an earlier real message).
            # A timer whose profile is missing, gone from the config, or no longer
            # the profile of that chat does not run. There is no fallback.
            is_group = self._conversation_db.is_group_conversation(conversation_id)
            profile, refused = profile_for_timer({**timer_data, "conversation_id": conversation_id}, is_group)
            # What was waiting in this chat when the fire's turn came (thread_controller
            # stacking): a person's messages and [ADMIN_TURN] wake-ups, answered in this run.
            extras = message.get("batch") or []
            if profile is None:
                log_message_flow("ChatController", "SYSTEM", "timer_refused", conversation_id,
                                 f"timer={timer_data.get('timer_id')} did not fire: {refused}")
                return self._answer_alone(extras)   # the fire is refused, the messages are not
            extras = [m for m in extras if not admin_turn.is_wake(m)
                      or (m.get("adminProfile") == profile.name and profile.admin)]
            # A person in a one-to-one chat is named as waiting for an answer.
            people = [] if is_group else [m for m in extras if not admin_turn.is_wake(m)]

            skip_session = bool(timer_data.get('skip_session', False))
            tool_name = "timer_fired"
            log_message_flow("ProactiveSystem", "ChatController", "timer_trigger", conversation_id,
                             f"timer={timer_data.get('timer_id')} profile={profile.name} skip_session={skip_session}")

            context_text = self._format_timer_context(timer_data)
            if is_group:
                context_text += "\n\n" + group_pace.note(conversation_id)

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
            for m in extras:
                self.thread_controller.store_message(m)

            service = self._service_for(profile)

            # Build system prompt
            system_prompt = self.get_system_prompt(profile)

            # Get conversation history (skip if skip_session)
            if skip_session:
                conversation_history = []
            else:
                conversation_history = self.thread_controller.get_chat_history(
                    conversation_id, limit=Config.COMPACTION_AFTER + 20)
                drop = 1 + len(extras)          # this turn's input is passed separately
                conversation_history = conversation_history[:-drop] if conversation_history else []
                conversation_history, context_note = compact_context(
                    conversation_history, service, conversation_id, self._conversation_db,
                    profile=profile, incoming=tool_return)
                if context_note:
                    system_prompt = system_prompt + context_note
                    tool_return['contextNote'] = context_note   # for Claude Code (no system prompt)

            # The stored fire stays the timer's own text; the model gets it with the
            # waiting messages under it.
            if extras:
                tool_return = {**tool_return,
                               "content": context_text + self._waiting_text(extras, people, is_group)}

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

            if self._caught_send_block(service_response, profile, service, conversation_id,
                                       "group" if is_group else "dm"):
                return None
            if self._caught_admin_turn(service_response, profile, service, conversation_id,
                                       "group" if is_group else "dm"):
                return None
            if self._caught_a2ui(service_response, profile, service, conversation_id,
                                 "group" if is_group else "dm"):
                return None
            # A timer fire may decide there is nothing worth saying (quiet hours,
            # silent upkeep). Replying with exactly NO_REPLY_TOKEN ends the run
            # without sending anything, also when a person's message rode along.
            if self._is_no_reply(service_response.get("content")):
                log_message_flow("ChatController", "SYSTEM", "timer_no_reply", conversation_id,
                                 f"timer={timer_data.get('timer_id')} answered {NO_REPLY_TOKEN}, nothing sent")
                return None
            if is_group and not self._group_may_send(conversation_id, service_response.get("content"), "timer"):
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

    def _caught_send_block(self, service_response, profile, service, conversation_id, kind):
        """True when the answer held a [SEND_XMTP_MESSAGE] block that was acted on:
        the result is queued for the model and nothing goes to the chat.

        Only a Claude Code run can take the result (it resumes its session with
        it). On another engine the block is cut out and the rest of the answer is
        delivered as usual: a block must never reach the chat as text."""
        content = service_response.get("content")
        text = content.get("value") if isinstance(content, dict) else content
        if not send_block.has_block(text):
            return False
        if not isinstance(service, ClaudeCodeService):
            rest = send_block.strip(text)
            log_message_flow("ChatController", "SYSTEM", "send_block_dropped", conversation_id,
                             "this engine cannot take a send result: block cut out")
            if not rest:
                return True
            if isinstance(content, dict):
                content["value"] = rest
            else:
                service_response["content"] = rest
            return False
        result = send_block.run(text, profile, conversation_id, on_sent=self._store_sent)
        log_message_flow("ChatController", "SYSTEM", "send_block", conversation_id, result.replace("\n", " | ")[:300])
        self.thread_controller.add_message(send_block.result_message(conversation_id, result, kind))
        return True

    def _apply_reply_block(self, final_response, conversation_id):
        """An answer that is a [REPLY] block (modules/reply_block.py) becomes a reply
        message; a tag that does not resolve leaves the text as a plain message."""
        content = final_response.get("content")
        text = content.get("value") if isinstance(content, dict) else content
        if not reply_block.has_block(text):
            return
        parsed = reply_block.parse(text)
        reference, why = (None, parsed.get('error'))
        if not why:
            reference, why = msg_tags.resolve(conversation_id, parsed['to'])
        message = parsed.get('message') or ''
        final_response["content"] = {"type": "text", "value": message}
        if why:
            log_message_flow("ChatController", "SYSTEM", "reply_block_plain", conversation_id,
                             f"{why}: delivered as a plain message")
            return
        final_response["contentType"] = "reply"
        final_response["reference"] = reference

    def _caught_a2ui(self, service_response, profile, service, conversation_id, kind):
        """True when the answer held an <a2ui> block and this method dealt with the
        whole answer (modules/a2ui_block.py):

        - every block passed the check: the answer's parts are queued in order,
          text as text and each surface as an `a2ui` message;
        - a block failed: its error is queued for the model, which writes again.

        False when the answer goes on as plain text: no block, or the block was
        cut out (a profile without `a2ui`, a group, an engine that cannot take
        the error back, or the fixes of this turn used up)."""
        content = service_response.get("content")
        text = content.get("value") if isinstance(content, dict) else content
        if not a2ui_block.has_block(text):
            return False

        def as_plain(rest, why):
            log_message_flow("ChatController", "SYSTEM", "a2ui_cut", conversation_id, why)
            if not rest:
                return True                 # nothing left to say
            if isinstance(content, dict):
                content["value"] = rest
            else:
                service_response["content"] = rest
            return False

        if not getattr(profile, 'a2ui', False):
            return as_plain(a2ui_block.strip(text), "this profile may not send a2ui: block cut out")
        if kind == "group":
            return as_plain(a2ui_block.strip(text), "a2ui is for private chats: block cut out")
        outcome, value = a2ui_block.run(text, conversation_id, a2ui_block.spec_of(profile))
        if outcome == 'retry' and not isinstance(service, ClaudeCodeService):
            return as_plain(a2ui_block.strip(text), "block refused and this engine cannot take the error back")
        if outcome == 'retry':
            log_message_flow("ChatController", "SYSTEM", "a2ui_refused", conversation_id,
                             " | ".join(value.splitlines())[:300])
            self.thread_controller.add_message(a2ui_block.result_message(conversation_id, value, kind))
            return True
        if outcome == 'plain':
            return as_plain(value, "block refused, no fixes left: answer sent without it")
        now = int(datetime.datetime.now().timestamp() * 1000)
        for i, part in enumerate(value):
            out = {
                "role": "assistant",
                "address": self.thread_controller.get_address(),
                "timestamp": now + i,
                "conversationId": conversation_id,
                "tool_calls": None,
                "token_count": service_response.get("token_count") if i == 0 else None,
                "source": "ChatController",
            }
            if 'a2ui' in part:
                out["content"] = {"type": "text", "value": a2ui_block.stored_text(part)}
                out["contentType"] = "a2ui"
                out["a2ui"] = part['a2ui']
                out["fallback"] = part.get('fallback') or ''
            else:
                out["content"] = {"type": "text", "value": part['text']}
            self.thread_controller.add_message(out)
        log_message_flow("ChatController", "SYSTEM", "a2ui_sent", conversation_id,
                         f"{sum(1 for p in value if 'a2ui' in p)} surface message(s), "
                         f"{sum(1 for p in value if 'text' in p)} text part(s)")
        return True

    def _caught_admin_turn(self, service_response, profile, service, conversation_id, kind):
        """True when nothing of this answer goes to the chat. An answer that ends with
        [ADMIN_TURN] (modules/admin_turn.py):

        - accepted: a wake-up is queued for the main; the text before the block stays
          as the answer (returns False so it is delivered), or nothing when there is none.
        - refused (the link, a copied note, the caps): nothing is delivered and the
          reason is queued for the model, which carries on. Only a Claude Code run can
          take it; on another engine the block is cut out and the text is delivered."""
        content = service_response.get("content")
        text = content.get("value") if isinstance(content, dict) else content
        if not admin_turn.has_block(text):
            return False
        rest = admin_turn.before(text)
        try:
            outcome = admin_turn.ask(profile, conversation_id, self._conversation_db)
        except Exception as e:                      # a failed request never ends the turn badly
            log_message_flow("ChatController", "SYSTEM", "admin_turn_error", conversation_id, str(e))
            outcome = {"wake": None, "error": None}
        refused = outcome.get("error")
        if refused and isinstance(service, ClaudeCodeService):
            log_message_flow("ChatController", "SYSTEM", "admin_turn_refused", conversation_id,
                             refused.replace("\n", " | ")[:200])
            self.thread_controller.add_message(admin_turn.result_message(conversation_id, refused, kind))
            return True
        if refused:
            log_message_flow("ChatController", "SYSTEM", "admin_turn_refused", conversation_id,
                             "this engine cannot take the reason: block cut out")
        elif outcome.get("wake"):
            log_message_flow("ChatController", "SYSTEM", "admin_turn", conversation_id,
                             f"profile {profile.name} woke {outcome['wake'].get('adminProfile')}")
            self.thread_controller.add_message(outcome["wake"])
        if not rest:
            return True
        if isinstance(content, dict):
            content["value"] = rest
        else:
            service_response["content"] = rest
        return False

    @staticmethod
    def _wake_ups_ok(message, profile):
        """False when this turn is nothing but [ADMIN_TURN] wake-ups that are not for
        `profile`. Wake-ups for another profile inside a stack are taken out of it."""
        def ok(m):
            return not admin_turn.is_wake(m) or (m.get("adminProfile") == profile.name and profile.admin)
        batch = message.get("batch")
        if not batch:
            return ok(message)
        kept = [m for m in batch if ok(m)]
        if not kept:
            return False
        if len(kept) != len(batch):
            message["batch"] = kept
        return True

    def _store_sent(self, conversation_id, block, profile=None):
        """Keep a sent message in its own chat's history, so whoever answers there
        later sees what was said. A first message to someone puts them on the inbox
        list for the sender's public profile and makes their folder. Returns text to
        add to the result line ('' = none)."""
        made = ""
        if block.get("kind") == "inbox":
            self._conversation_db.commit_conversation(conversation_id, "dm")
            if profile is not None:
                made = register_outreach(profile, block.get("target"))
        self.thread_controller.store_message({
            "role": "assistant",
            "address": self.thread_controller.get_address(),
            "conversationId": conversation_id,
            "content": {"type": "text", "value": block.get("message", "")},
            "timestamp": int(datetime.datetime.now().timestamp() * 1000),
            "source": send_block.SOURCE,
        })
        return made

    def _group_may_send(self, conversation_id, content, what):
        """True (and the post is counted) when a group answer goes out."""
        if self._is_no_reply(content):
            log_message_flow("ChatController", "SYSTEM", f"{what}_no_reply", conversation_id,
                             f"answered {NO_REPLY_TOKEN}, nothing sent")
            return False
        if not group_pace.allowed(conversation_id):
            log_message_flow("ChatController", "SYSTEM", f"{what}_held", conversation_id,
                             "posted here less than group_reply_secs ago: answer not sent")
            return False
        group_pace.mark(conversation_id)
        return True

    def _answer_alone(self, extras):
        """Messages that were stacked onto a timer fire that did not run: they get
        their own turn, as if the fire had not been there."""
        if not extras:
            return None
        message = self.thread_controller.as_batch(extras)
        if len(extras) == 1:
            self.thread_controller.store_message(message)
        return self.receive_Message_ChatController(message)

    def _waiting_text(self, extras, people, is_group=False):
        """What is added under a timer fire's text when messages of the same chat
        were waiting: each one, in order (in a group, with its sender's name), and
        what that means for the answer."""
        lines = []
        for m in extras:
            c = m.get("content")
            text = c.get("value", "") if isinstance(c, dict) else str(c or "")
            if is_group and not admin_turn.is_wake(m):
                name, _ = self._resolve_sender_name(m.get("senderInboxId"))
                text = f"{name or 'someone'}: {text}"
            lines.append(text)
        text = ("\n\nAlso waiting in this chat when the timer fired. Handle these in this same "
                "turn, after or along with the timer's work:\n" + "\n".join(lines))
        if people:
            text += "\n\nA person wrote: end this turn with your answer to them."
        return text

    def _merge_batch(self, message, batch, is_group):
        """Several messages of one chat answered in one turn: one text, in order.
        In a group each line starts with its sender's name."""
        lines = []
        for m in batch:
            c = m.get("content")
            text = c.get("value", "") if isinstance(c, dict) else str(c or "")
            if is_group:
                name, _ = self._resolve_sender_name(m.get("senderInboxId"))
                text = f"{name or 'someone'}: {text}"
            lines.append(text)
        message["content"] = {"type": "text", "value": "\n".join(lines)}
        message["prefixed"] = is_group

    @staticmethod
    def _append_text(message, line):
        c = message.get("content")
        if isinstance(c, dict) and c.get("type") == "text":
            message["content"] = {**c, "value": f"{c.get('value', '')}\n\n{line}"}

    @staticmethod
    def _is_no_reply(content) -> bool:
        """True when a response means "send nothing": the no-reply token alone,
        or the token on its own last line. Models often write a closing note
        and then the token on a line of its own ("Nothing to report this
        turn." + a blank line + the token); that note is meant for nobody, so
        the whole answer is dropped. A token in the middle of a sentence does
        not count."""
        text = content.get("value") if isinstance(content, dict) else content
        return is_no_reply_text(text)

    def _format_timer_context(self, timer: dict) -> str:
        """Format a timer's data into prompt text for the AI"""
        description = timer.get('description', 'Timer')
        instructions = timer.get('instructions', '')
        timer_type = timer.get('timer_type', 'one_shot')

        recurring_note = " (Recurring timer, will fire again automatically.)" if timer_type == 'recurring' else ""

        return f"Timer fired: {description}. Instructions: {instructions}{recurring_note}"