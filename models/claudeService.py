import json
import logging
import time
import os
import base64
from typing import Optional

import anthropic

from config import Config
from models.base_llm_service import BaseLLMService
from utils.message_logger import log_message_flow, truncate_message

logger = logging.getLogger(__name__)


class ClaudeService(BaseLLMService):
    def __init__(self):
        log_message_flow("SYSTEM", "ClaudeService", "service_init", None, "initializing")
        api_key = os.environ.get("ANTHROPICS_API_KEY") or Config.ANTROKEY
        if not api_key:
            raise ValueError("ANTHROPICS_API_KEY not set.")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = Config.LLM_MODELS.get("claude", "claude-sonnet-4-5-20250929")
        log_message_flow("SYSTEM", "ClaudeService", "service_ready", None, f"initialized with {self.model}")

    def translate_history_to_claude_format(self, history: list) -> list:
        """Convert internal message history to Anthropic messages format.

        Anthropic requires strictly alternating user/assistant messages.
        System messages are not allowed in the messages list (passed separately).
        """
        claude_messages = []
        for message in history:
            role = message.get("role", "user")

            # Skip system messages — they go via the system param
            if role == "system":
                # If it's a tool result, convert to user message
                content = message.get("content")
                if isinstance(content, dict) and content.get("type") == "functionReturn":
                    value = content.get("value", "No result provided.")
                    claude_messages.append({
                        "role": "user",
                        "content": f"[Function returned: {value}]"
                    })
                continue

            # Tool role messages (from function_registry) → user message
            if role == "tool":
                tool_content = message.get("content", "")
                if isinstance(tool_content, dict):
                    tool_content = tool_content.get("value", str(tool_content))
                tool_call_id = message.get("tool_call_id", "unknown")
                claude_messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": str(tool_content)}]
                })
                continue

            claude_role = "assistant" if role == "assistant" else "user"
            content = message.get("content")
            parts = []

            # Handle attachments (images)
            is_attachment = message.get("contentType") == "attachment" or (
                isinstance(content, dict) and content.get("type") == "attachment"
            )

            if is_attachment and isinstance(content, dict):
                image_data = content.get("data")
                mime_type = content.get("mimeType") or message.get("mimeType") or "image/jpeg"
                text_value = content.get("value")

                if image_data and isinstance(image_data, str):
                    parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_data
                        }
                    })
                if text_value:
                    parts.append({"type": "text", "text": str(text_value)})

            elif isinstance(content, dict):
                content_type = content.get("type", "")
                value = content.get("value", "")
                if content_type == "functionReturn":
                    parts.append({"type": "text", "text": f"Function returned: {value}"})
                elif value:
                    parts.append({"type": "text", "text": str(value)})
            elif isinstance(content, str):
                parts.append({"type": "text", "text": content})
            elif content:
                parts.append({"type": "text", "text": str(content)})

            # Handle tool_calls from assistant messages (re-inject as tool_use blocks)
            tool_calls = message.get("tool_calls")
            if tool_calls and claude_role == "assistant":
                if isinstance(tool_calls, str):
                    try:
                        tool_calls = json.loads(tool_calls)
                    except json.JSONDecodeError:
                        tool_calls = []
                for tc in (tool_calls or []):
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    parts.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"call_{int(time.time())}"),
                        "name": func.get("name", "unknown"),
                        "input": args
                    })

            if parts:
                claude_messages.append({"role": claude_role, "content": parts})

        # Ensure alternating roles (Anthropic requirement)
        claude_messages = self._ensure_alternating_roles(claude_messages)
        return claude_messages

    def _ensure_alternating_roles(self, messages: list) -> list:
        """Merge consecutive same-role messages to satisfy Anthropic's alternating requirement."""
        if not messages:
            return messages

        merged = [messages[0]]
        for msg in messages[1:]:
            if msg["role"] == merged[-1]["role"]:
                # Merge content
                prev_content = merged[-1]["content"]
                curr_content = msg["content"]

                # Normalize both to lists
                if isinstance(prev_content, str):
                    prev_content = [{"type": "text", "text": prev_content}]
                if isinstance(curr_content, str):
                    curr_content = [{"type": "text", "text": curr_content}]
                if isinstance(prev_content, list) and isinstance(curr_content, list):
                    merged[-1]["content"] = prev_content + curr_content
                else:
                    # Fallback: just add as new message anyway
                    merged.append(msg)
            else:
                merged.append(msg)
        return merged

    def _convert_tools_to_anthropic(self, functions_list: str) -> list:
        """Convert OpenAI tools format to Anthropic tools format."""
        if not functions_list:
            return []

        try:
            tools_data = json.loads(functions_list)
        except json.JSONDecodeError:
            return []

        anthropic_tools = []
        for tool in tools_data:
            if isinstance(tool, dict) and tool.get("type") == "function":
                func = tool["function"]
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                })
        return anthropic_tools

    def _convert_tool_use_to_openai_format(self, response) -> list:
        """Convert Anthropic tool_use blocks to OpenAI tool_calls format."""
        tool_calls = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input) if isinstance(block.input, dict) else str(block.input)
                    }
                })
        return tool_calls or None

    def send_message(self, message_data: dict, conversation_history: list,
                     system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        sender_id = message_data.get('senderInboxId', 'unknown')
        content_preview = truncate_message(message_data.get('content'))
        log_message_flow("ChatController", "ClaudeService", "send_message", sender_id, content_preview)

        conversation_id = message_data.get("conversationId")
        timestamp = int(time.time())

        try:
            # Build messages (skip history for skip_session — clean slate, keep system prompt)
            claude_messages = self.translate_history_to_claude_format([] if skip_session else conversation_history)

            # Build current message — respect role: "tool" from function returns
            if message_data.get("role") == "tool":
                tool_content = message_data.get("content", "")
                if isinstance(tool_content, dict):
                    tool_content = tool_content.get("value", str(tool_content))
                claude_messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": message_data.get("tool_call_id", "unknown"), "content": str(tool_content)}]
                })
            else:
                current_parts = []
                image_data, mime_type, image_text = self._extract_image_from_content(message_data)

                if image_data and isinstance(image_data, str):
                    current_parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_data
                        }
                    })
                    if image_text:
                        current_parts.append({"type": "text", "text": str(image_text)})
                else:
                    text = self._extract_text_from_content(message_data.get("content"))
                    if text:
                        current_parts.append({"type": "text", "text": text})

                if not current_parts:
                    current_parts.append({"type": "text", "text": "..."})

                claude_messages.append({"role": "user", "content": current_parts})
            claude_messages = self._ensure_alternating_roles(claude_messages)

            # Build tools
            tools = self._convert_tools_to_anthropic(functions_list)

            # API call
            log_message_flow("ClaudeService", "Claude_API", "api_request", conversation_id, "sending message")
            kwargs = {
                "model": self.model,
                "max_tokens": Config.LLM_MAX_TOKENS,
                "system": system_prompt,
                "messages": claude_messages,
            }
            if tools:
                kwargs["tools"] = tools

            response = self.client.messages.create(**kwargs)
            log_message_flow("Claude_API", "ClaudeService", "api_response", conversation_id, "received response")

            # Extract text
            raw_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    raw_text += block.text

            # Extract tool calls
            tool_calls = self._convert_tool_use_to_openai_format(response)
            if tool_calls:
                first_name = tool_calls[0]["function"]["name"]
                log_message_flow("ClaudeService", "FunctionRegistry", "tool_call", conversation_id, first_name)

            # Token count
            token_count = 0
            if response.usage:
                token_count = response.usage.input_tokens + response.usage.output_tokens

            # Try to parse JSON response (same pattern as Gemini)
            try:
                clean_text = raw_text.strip()
                if clean_text.startswith("```json"):
                    clean_text = clean_text.replace("```json", "", 1)
                if clean_text.endswith("```"):
                    clean_text = clean_text[:-3]
                clean_text = clean_text.strip()

                parsed_response = json.loads(clean_text)
                return {
                    "role": "assistant",
                    "content": parsed_response.get("content", {"type": "text", "value": raw_text}),
                    "timestamp": timestamp,
                    "conversationId": conversation_id,
                    "tool_calls": tool_calls,
                    "token_count": token_count
                }
            except json.JSONDecodeError:
                return {
                    "role": "assistant",
                    "content": {"type": "text", "value": raw_text},
                    "timestamp": timestamp,
                    "conversationId": conversation_id,
                    "tool_calls": tool_calls,
                    "token_count": token_count
                }

        except Exception as e:
            log_message_flow("ClaudeService", "SYSTEM", "error", conversation_id, str(e))
            return self._create_error_response(f"Service Error: {str(e)}", conversation_id, timestamp)

    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        try:
            resp = self.client.messages.create(
                model=model or "claude-haiku-4-5",
                max_tokens=max_tokens,
                system="You are a helpful assistant. Be concise.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = ""
            for block in resp.content:
                if hasattr(block, "text"):
                    text += block.text
            return text.strip() or None
        except Exception as e:
            logger.warning(f"[ClaudeService.quick_call] {e}")
            return None
