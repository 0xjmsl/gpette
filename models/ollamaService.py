import json
import logging
import time
import os
from typing import Optional

import ollama

from config import Config
from models.base_llm_service import BaseLLMService
from utils.message_logger import log_message_flow, truncate_message

logger = logging.getLogger(__name__)


class OllamaService(BaseLLMService):
    def __init__(self, host: str = None):
        log_message_flow("SYSTEM", "OllamaService", "service_init", None, "initializing")
        self._explicit_host = host  # None means "follow Config live"
        self._active_host = host or Config.OLLAMA_HOST
        self.model = Config.LLM_MODELS.get("ollama", "qwen3")
        try:
            self.client = ollama.Client(host=self._active_host)
            self.client.list()
            log_message_flow("SYSTEM", "OllamaService", "service_ready", None, f"connected to {self._active_host}")
        except Exception as e:
            raise ConnectionError(f"Could not connect to Ollama at {self._active_host}: {e}") from e

    def _ensure_client(self):
        """Reconnect if Config.OLLAMA_HOST changed since last call."""
        if self._explicit_host:
            return  # Pinned host, don't follow Config
        current = Config.OLLAMA_HOST
        if current != self._active_host:
            self._active_host = current
            self.client = ollama.Client(host=current)
            log_message_flow("SYSTEM", "OllamaService", "host_changed", None, f"reconnected to {current}")

    def translate_history_to_ollama_format(self, history: list) -> list:
        """Convert internal message history to Ollama's message list format."""
        ollama_history = []
        for message in history:
            role = message.get("role", "user")
            if role not in ["user", "assistant", "system", "tool"]:
                role = "assistant"

            content_text = ""
            images_b64 = []

            content = message.get("content")

            if isinstance(content, str):
                content_text = content
            elif isinstance(content, dict):
                content_type = content.get("type")
                if message.get("contentType") == "attachment" or content_type == "attachment":
                    if isinstance(content.get("data"), str):
                        images_b64.append(content["data"])
                    if content.get("value"):
                        content_text = str(content["value"])
                elif content_type == "functionReturn":
                    value = content.get("value", "No result provided.")
                    content_text = f"Function returned: {value}"
                elif content.get("value"):
                    content_text = str(content.get("value"))

            ollama_message = {
                "role": role,
                "content": content_text
            }
            if images_b64:
                ollama_message["images"] = images_b64

            # Re-inject tool_calls for assistant messages
            tool_calls = message.get("tool_calls")
            if tool_calls and role == "assistant":
                if isinstance(tool_calls, str):
                    try:
                        tool_calls = json.loads(tool_calls)
                    except json.JSONDecodeError:
                        tool_calls = []
                # Convert OpenAI format back to Ollama format for history
                if tool_calls:
                    ollama_tool_calls = []
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        args = func.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        ollama_tool_calls.append({
                            "function": {
                                "name": func.get("name"),
                                "arguments": args
                            }
                        })
                    ollama_message["tool_calls"] = ollama_tool_calls

            ollama_history.append(ollama_message)

        return ollama_history

    def format_functions_for_ollama(self, function_descriptions: str) -> list:
        """Convert OpenAI tools format to Ollama tools format."""
        if not function_descriptions:
            return []

        tools = []
        try:
            tools_data = json.loads(function_descriptions)
            for tool in tools_data:
                if isinstance(tool, dict) and tool.get("type") == "function":
                    tools.append(tool)
        except json.JSONDecodeError as e:
            print(f"Error parsing function descriptions JSON: {e}")
        return tools

    def send_message(self, message_data: dict, conversation_history: list,
                     system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        sender_id = message_data.get('senderInboxId', 'unknown')
        content_preview = truncate_message(message_data.get('content'))
        log_message_flow("ChatController", "OllamaService", "send_message", sender_id, content_preview)

        conversation_id = message_data.get("conversationId")
        timestamp = int(time.time())

        self._ensure_client()

        if not self.client:
            return self._create_error_response("Ollama client not initialized.", conversation_id, timestamp)

        try:
            messages = self.translate_history_to_ollama_format([] if skip_session else conversation_history)

            # Add system prompt
            if not any(m['role'] == 'system' for m in messages):
                messages.insert(0, {'role': 'system', 'content': system_prompt})

            # Add current user message
            current_message = self.translate_history_to_ollama_format([message_data])[0]
            messages.append(current_message)

            tools = self.format_functions_for_ollama(functions_list)

            request_payload = {
                'model': self.model,
                'messages': messages,
                'stream': False
            }

            if tools:
                request_payload['tools'] = tools

            log_message_flow("OllamaService", "Ollama_API", "api_request", conversation_id, "sending message")
            response = self.client.chat(**request_payload)
            log_message_flow("Ollama_API", "OllamaService", "api_response", conversation_id, "received response")

            response_message = response.get('message', {})
            content = response_message.get('content', '')
            raw_tool_calls = response_message.get('tool_calls')

            # Convert Ollama tool_calls to OpenAI format
            tool_calls = None
            if raw_tool_calls:
                tool_calls = []
                for i, tc in enumerate(raw_tool_calls):
                    func = tc.get('function', {})
                    args = func.get('arguments', {})
                    # Ollama returns arguments as dict, OpenAI format needs JSON string
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    tool_calls.append({
                        "id": f"call_ollama_{int(time.time())}_{i}",
                        "type": "function",
                        "function": {
                            "name": func.get('name', ''),
                            "arguments": args
                        }
                    })
                if tool_calls:
                    first_name = tool_calls[0]["function"]["name"]
                    log_message_flow("OllamaService", "FunctionRegistry", "tool_call", conversation_id, first_name)

            # Try to parse JSON response
            try:
                parsed_content = json.loads(content)
                final_content = parsed_content.get("content", {"type": "text", "value": content})
            except (json.JSONDecodeError, TypeError):
                final_content = {"type": "text", "value": content}

            token_count = response.get('eval_count', 0)

            return {
                "role": "assistant",
                "content": final_content,
                "timestamp": timestamp,
                "conversationId": conversation_id,
                "tool_calls": tool_calls,
                "token_count": token_count
            }

        except Exception as e:
            log_message_flow("OllamaService", "SYSTEM", "error", conversation_id, str(e))
            return self._create_error_response(f"Service Error: {str(e)}", conversation_id, timestamp)

    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        try:
            self._ensure_client()
            resp = self.client.chat(
                model=model or self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
                options={"num_predict": max_tokens, "temperature": 0.3},
            )
            content = resp.get('message', {}).get('content', '') or ""
            if isinstance(content, str) and "</think>" in content:
                content = content.split("</think>")[-1].strip()
            return content.strip() or None
        except Exception as e:
            logger.warning(f"[OllamaService.quick_call] {e}")
            return None
