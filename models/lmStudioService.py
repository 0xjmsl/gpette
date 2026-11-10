import json
import datetime
import logging
import os
import requests
import time
from typing import Optional

from config import Config
from models.base_llm_service import BaseLLMService
from utils.message_logger import log_message_flow, truncate_message

logger = logging.getLogger(__name__)


class LMStudioService(BaseLLMService):
    """
    A service to interact with a local model served by LM Studio,
    using an OpenAI-compatible API. This version handles the renaming of
    the 'functionName' key to 'name' for tool definitions and unwraps
    nested JSON responses from the model.
    """
    def __init__(self, host: str = None):
        """
        Initializes the LMStudioService.

        Args:
            host (str, optional): The base URL of the LM Studio server.
                                  Defaults to the value of the LM_STUDIO_HOST
                                  environment variable or 'http://localhost:1234/v1'
                                  if not set.
        """
        log_message_flow("SYSTEM", "LMStudioService", "service_init", None, "initializing")
        self._explicit_host = host  # None means "follow Config live"
        active_host = host or Config.LM_STUDIO_HOST
        # [llm.models] lmstudio, or a profile's `model`; empty = the server's loaded model.
        self.model = Config.LLM_MODELS.get("lmstudio") or ""

        try:
            response = requests.get(f"{active_host}/models")
            response.raise_for_status()
            log_message_flow("SYSTEM", "LMStudioService", "service_ready", None, f"connected to {active_host}")
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Could not connect to LM Studio at {active_host}: {e}") from e

    @property
    def host(self):
        return self._explicit_host or Config.LM_STUDIO_HOST

    @property
    def chat_url(self):
        return f"{self.host}/chat/completions"

    def send_message(self, message_data: dict, conversation_history: list, system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        """
        Processes a message, calls the LM Studio API, and returns a formatted response.

        Args:
            message_data (dict): The current message from the user.
            conversation_history (list): The preceding messages in the conversation.
            system_prompt (str): The system prompt to guide the model.
            functions_list (str, optional): A JSON string of available functions/tools,
                                            using OpenAI tools format.
            skip_session (bool): If True, ignore conversation history (clean slate).

        Returns:
            dict: A standardized dictionary containing the model's response.
        """
        conversation_id = message_data.get("conversationId")
        now = datetime.datetime.now()
        timestamp = int(now.timestamp() * 1000)

        try:
            # --- 1. Translate History and Current Message to OpenAI Format ---
            messages = []
            if system_prompt:
                messages.append({'role': 'system', 'content': system_prompt})

            full_history = ([] if skip_session else conversation_history) + [message_data]

            for message in full_history:
                role = message.get("role", "user")
                if role not in ["user", "assistant", "system", "tool"]:
                    role = "assistant"

                content = message.get("content")
                
                # Handle different message types
                if role == "tool":
                    # Tool response message
                    tool_call_id = message.get("tool_call_id", "unknown_tool_call")
                    if isinstance(content, str):
                        tool_content = content
                    elif isinstance(content, dict):
                        tool_content = content.get("value", str(content))
                    else:
                        tool_content = str(content)
                    
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_content
                    })
                elif isinstance(content, str):
                    messages.append({"role": role, "content": content})
                elif isinstance(content, dict):
                    content_type = content.get("type")
                    if content_type == "functionReturn":
                        # Legacy function return - convert to tool format if we have tool_call_id
                        tool_call_id = message.get("tool_call_id", "unknown_tool_call")
                        value = content.get("value", "No result provided.")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": str(value)
                        })
                    elif message.get("contentType") == "attachment" or content_type == "attachment":
                        text_part = str(content.get("value", ""))
                        image_b64 = content.get("data")
                        mime_type = content.get("mimeType", "image/jpeg")
                        msg_content_parts = [{"type": "text", "text": text_part}]
                        if image_b64 and mime_type.startswith("image/"):
                            msg_content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}
                            })
                        messages.append({"role": role, "content": msg_content_parts})
                    else:
                        messages.append({"role": role, "content": str(content.get("value", ""))})

                # Handle tool_calls (OpenAI format)
                if message.get("tool_calls"):
                    tool_calls = message.get("tool_calls")
                    if isinstance(tool_calls, str):
                        try:
                            tool_calls = json.loads(tool_calls)
                        except json.JSONDecodeError:
                            tool_calls = []
                    
                    if tool_calls and isinstance(tool_calls, list):
                        # Find the last assistant message and add tool_calls
                        for msg in reversed(messages):
                            if msg["role"] == "assistant":
                                msg["tool_calls"] = tool_calls
                                break
                
                # Handle legacy function_call format
                elif message.get("function_call"):
                    function_call = message["function_call"]
                    # Convert to tool_calls format
                    tool_call = {
                        "id": f"call_{int(time.time())}",
                        "type": "function",
                        "function": {
                            "name": function_call.get("name"),
                            "arguments": json.dumps(function_call.get("parameters", {}))
                        }
                    }
                    
                    for msg in reversed(messages):
                        if msg["role"] == "assistant":
                            msg["tool_calls"] = [tool_call]
                            break

            # --- 2. Parse OpenAI Tools Format ---
            tools = []
            if functions_list:
                try:
                    # Check if functions_list is already in OpenAI tools format
                    parsed_functions = json.loads(functions_list)
                    
                    if isinstance(parsed_functions, list) and parsed_functions:
                        first_item = parsed_functions[0]
                        
                        # If it's already in OpenAI format (has "type": "function")
                        if isinstance(first_item, dict) and first_item.get("type") == "function":
                            tools = parsed_functions
                        
                        # If it's legacy format (has "functionName")
                        elif isinstance(first_item, dict) and "functionName" in first_item:
                            for i, original_func_def in enumerate(parsed_functions):
                                if 'functionName' not in original_func_def or not original_func_def['functionName']:
                                    print(f"Warning: Function at index {i} is missing a 'functionName' field and will be skipped.")
                                    continue
                                
                                api_compliant_func = {
                                    "name": original_func_def["functionName"],
                                    "description": original_func_def.get("description", ""),
                                    "parameters": original_func_def.get("parameters", {"type": "object", "properties": {}})
                                }
                                
                                tools.append({
                                    'type': 'function',
                                    'function': api_compliant_func,
                                })
                        
                except json.JSONDecodeError as e:
                    print(f"Warning: Could not parse functions_list JSON. Ensure it is a valid JSON string. Error: {e}")

            # --- 3. Build Request Payload and Call API ---
            request_payload = {
                'messages': messages,
                'stream': False,
                "temperature": Config.LLM_TEMPERATURE,
                "max_tokens": Config.LLM_MAX_TOKENS,
            }
            if self.model:
                request_payload['model'] = self.model
            if tools:
                request_payload['tools'] = tools
                request_payload['tool_choice'] = 'auto'

            print("Sending message to LM Studio...")
            response = requests.post(self.chat_url, json=request_payload, timeout=Config.LLM_REQUEST_TIMEOUT)
            response.raise_for_status()
            response_data = response.json()
            print("Received response from LM Studio.")

            # --- 4. Parse Response and Format for Return ---
            response_message = response_data['choices'][0]['message']
            content = response_message.get('content', '')
            tool_calls = response_message.get('tool_calls', [])

            # Remove everything before and including the last </think> tag, if present
            if isinstance(content, str) and '</think>' in content:
                content = content.split('</think>')[-1].lstrip()
            token_count = response_data.get('usage', {}).get('total_tokens', 0)

            # Format content properly
            if isinstance(content, dict) and 'value' in content:
                final_content = content
            elif isinstance(content, str) and content.strip():
                final_content = {"type": "text", "value": content}
            else:
                final_content = {"type": "text", "value": str(content) if content else ""}

            # Handle tool_calls from response
            final_tool_calls = None
            if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                final_tool_calls = tool_calls

            return {
                "role": "assistant",
                "content": final_content,
                "timestamp": timestamp,
                "conversationId": conversation_id,
                "tool_calls": final_tool_calls,
                "token_count": token_count
            }

        except requests.exceptions.RequestException as e:
            print(f"ERROR calling LM Studio API: {e}")
            return self._create_error_response(f"API Error: {str(e)}", conversation_id, timestamp)
        except Exception as e:
            print(f"ERROR in LMStudioService send_message: {e}")
            return self._create_error_response(f"Service Error: {str(e)}", conversation_id, timestamp)

    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        try:
            payload = {
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "stream": False,
            }
            if model:
                payload["model"] = model

            response = requests.post(self.chat_url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(content, str) and "</think>" in content:
                content = content.split("</think>")[-1].strip()
            return content.strip() or None
        except Exception as e:
            logger.warning(f"[LMStudioService.quick_call] {e}")
            return None

