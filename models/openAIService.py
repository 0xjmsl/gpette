import json
import logging
import time
import os
from typing import Optional

from openai import OpenAI

from config import Config
from models.base_llm_service import BaseLLMService
from utils.message_logger import log_message_flow, truncate_message

logger = logging.getLogger(__name__)


class OpenAIService(BaseLLMService):
    def __init__(self):
        log_message_flow("SYSTEM", "OpenAIService", "service_init", None, "initializing")
        api_key = os.environ.get("OPENAI_API_KEY") or Config.OPENAI_API_KEY
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set.")
        self.client = OpenAI(api_key=api_key)
        self.model = Config.LLM_MODELS.get("openai", "gpt-4.1")
        log_message_flow("SYSTEM", "OpenAIService", "service_ready", None, f"initialized with {self.model}")

    def translate_history_to_openai_format(self, history: list, system_prompt: str) -> list:
        """Convert internal message history to OpenAI messages format."""
        openai_messages = [{"role": "system", "content": system_prompt}]

        for message in history:
            role = message.get("role", "user")

            # Tool role messages pass through directly
            if role == "tool":
                tool_content = message.get("content", "")
                if isinstance(tool_content, dict):
                    tool_content = tool_content.get("value", str(tool_content))
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id", "unknown"),
                    "content": str(tool_content)
                })
                continue

            # System messages with function returns
            if role == "system":
                content = message.get("content")
                if isinstance(content, dict) and content.get("type") == "functionReturn":
                    value = content.get("value", "No result provided.")
                    # Check if there's a tool_call_id
                    tool_call_id = message.get("tool_call_id")
                    if tool_call_id:
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": str(value)
                        })
                    else:
                        openai_messages.append({
                            "role": "user",
                            "content": f"[Function returned: {value}]"
                        })
                continue

            openai_role = role if role in ["user", "assistant"] else "user"
            content = message.get("content")

            # Handle attachments (images)
            is_attachment = message.get("contentType") == "attachment" or (
                isinstance(content, dict) and content.get("type") == "attachment"
            )

            if is_attachment and isinstance(content, dict):
                image_data = content.get("data")
                mime_type = content.get("mimeType") or message.get("mimeType") or "image/jpeg"
                text_value = content.get("value")
                parts = []

                if text_value:
                    parts.append({"type": "text", "text": str(text_value)})
                if image_data and isinstance(image_data, str):
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_data}"}
                    })
                if parts:
                    openai_messages.append({"role": openai_role, "content": parts})
            elif isinstance(content, dict):
                content_type = content.get("type", "")
                value = content.get("value", "")
                if content_type == "functionReturn":
                    openai_messages.append({"role": "user", "content": f"Function returned: {value}"})
                elif value:
                    openai_messages.append({"role": openai_role, "content": str(value)})
            elif isinstance(content, str):
                openai_messages.append({"role": openai_role, "content": content})
            elif content:
                openai_messages.append({"role": openai_role, "content": str(content)})

            # Re-inject tool_calls on assistant messages
            tool_calls = message.get("tool_calls")
            if tool_calls and openai_role == "assistant":
                if isinstance(tool_calls, str):
                    try:
                        tool_calls = json.loads(tool_calls)
                    except json.JSONDecodeError:
                        tool_calls = []
                if tool_calls:
                    # Find the last assistant message and add tool_calls
                    for msg in reversed(openai_messages):
                        if msg["role"] == "assistant":
                            msg["tool_calls"] = tool_calls
                            break

        return openai_messages

    def send_message(self, message_data: dict, conversation_history: list,
                     system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        sender_id = message_data.get('senderInboxId', 'unknown')
        content_preview = truncate_message(message_data.get('content'))
        log_message_flow("ChatController", "OpenAIService", "send_message", sender_id, content_preview)

        conversation_id = message_data.get("conversationId")
        timestamp = int(time.time())

        try:
            # Build messages (skip history for skip_session — clean slate, keep system prompt)
            openai_messages = self.translate_history_to_openai_format([] if skip_session else conversation_history, system_prompt)

            # Build current message — respect role: "tool" from function returns
            if message_data.get("role") == "tool":
                tool_content = message_data.get("content", "")
                if isinstance(tool_content, dict):
                    tool_content = tool_content.get("value", str(tool_content))
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": message_data.get("tool_call_id", "unknown"),
                    "content": str(tool_content)
                })
            else:
                image_data, mime_type, image_text = self._extract_image_from_content(message_data)

                if image_data and isinstance(image_data, str):
                    parts = []
                    if image_text:
                        parts.append({"type": "text", "text": str(image_text)})
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_data}"}
                    })
                    openai_messages.append({"role": "user", "content": parts})
                else:
                    text = self._extract_text_from_content(message_data.get("content"))
                    openai_messages.append({"role": "user", "content": text or "..."})

            # Parse tools
            tools = None
            if functions_list:
                try:
                    tools = json.loads(functions_list)
                except json.JSONDecodeError:
                    tools = None

            # API call
            log_message_flow("OpenAIService", "OpenAI_API", "api_request", conversation_id, "sending message")
            kwargs = {
                "model": self.model,
                "messages": openai_messages,
                "max_tokens": Config.LLM_MAX_TOKENS,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            response = self.client.chat.completions.create(**kwargs)
            log_message_flow("OpenAI_API", "OpenAIService", "api_response", conversation_id, "received response")

            response_message = response.choices[0].message
            raw_text = response_message.content or ""

            # Extract tool calls (already in OpenAI format)
            tool_calls = None
            if response_message.tool_calls:
                tool_calls = []
                for tc in response_message.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    })
                first_name = tool_calls[0]["function"]["name"]
                log_message_flow("OpenAIService", "FunctionRegistry", "tool_call", conversation_id, first_name)

            # Token count
            token_count = response.usage.total_tokens if response.usage else 0

            # Try to parse JSON response
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
            log_message_flow("OpenAIService", "SYSTEM", "error", conversation_id, str(e))
            return self._create_error_response(f"Service Error: {str(e)}", conversation_id, timestamp)

    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        try:
            resp = self.client.chat.completions.create(
                model=model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.3,
            )
            content = resp.choices[0].message.content or ""
            return content.strip() or None
        except Exception as e:
            logger.warning(f"[OpenAIService.quick_call] {e}")
            return None
