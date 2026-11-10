import base64
import json
import logging
import time
import os
import re
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types

from config import Config
from models.base_llm_service import BaseLLMService
from utils.message_logger import log_message_flow, truncate_message

logger = logging.getLogger(__name__)

class GeminiService(BaseLLMService):
    def __init__(self):
        log_message_flow("SYSTEM", "GeminiService", "service_init", None, "initializing")
        self.client = None
        self.api_key = os.environ.get("GEMINI_API_KEY")

        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set.")

        self.client = genai.Client(api_key=self.api_key)
        self.model = Config.LLM_MODELS.get("gemini", "gemini-2.5-flash")
        log_message_flow("SYSTEM", "GeminiService", "service_ready", None, f"initialized with {self.model}")

    def translate_history_to_gemini_format(self, history: list) -> list:
        """Convert internal message history to Gemini's Content list format."""
        gemini_history = []
        for message in history:
            role = "model" if message.get("role") in ["assistant", "system"] else "user"
            parts = []
            content = message.get("content")

            if content:
                is_attachment = message.get("contentType") == "attachment" or (isinstance(content, dict) and content.get("type") == "attachment")

                if is_attachment:
                    mime_type = content.get("mimeType")
                    image_data_b64 = content.get("data") if isinstance(content, dict) else None
                    text_value = content.get("value") if isinstance(content, dict) else None

                    if mime_type and image_data_b64 and isinstance(image_data_b64, str):
                        try:
                            image_bytes = base64.b64decode(image_data_b64)
                            parts.append(types.Part.from_bytes(mime_type=mime_type, data=image_bytes))
                        except Exception as e:
                            print(f"Warning: Could not decode base64 image data in history: {e}")

                    if text_value:
                        parts.append(types.Part.from_text(text=str(text_value)))

                elif isinstance(content, dict):
                    content_type = content.get("type", "")
                    value = content.get("value", "")
                    if content_type == "functionReturn":
                        parts.append(types.Part.from_text(text=f"Function returned: {value}"))
                    elif value:
                        parts.append(types.Part.from_text(text=str(value)))
                elif isinstance(content, str):
                    parts.append(types.Part.from_text(text=content))
                else:
                    parts.append(types.Part.from_text(text=str(content)))

            # Handle tool_calls from history (new format)
            tool_calls = message.get("tool_calls")
            if tool_calls and role == "model":
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
                    parts.append(types.Part.from_text(
                        text=f"[The assistant called function '{func.get('name', 'unknown')}' with parameters: {json.dumps(args)}]"
                    ))

            # Legacy: handle function_call from old history entries
            function_call = message.get("function_call")
            if function_call and role == "model" and not tool_calls:
                try:
                    if isinstance(function_call, str):
                        function_call = json.loads(function_call)
                    function_name = function_call.get("name", "unknown_function")
                    parameters = function_call.get("parameters", {})
                    if isinstance(parameters, str):
                        try:
                            parameters = json.loads(parameters) if parameters and parameters != "None" else {}
                        except json.JSONDecodeError:
                            parameters = {}
                    parts.append(types.Part.from_text(
                        text=f"[The assistant called function '{function_name}' with parameters: {json.dumps(parameters)}]"
                    ))
                except (json.JSONDecodeError, TypeError) as e:
                    print(f"Warning: Could not parse function call: {e}")

            if parts:
                gemini_history.append(types.Content(role=role, parts=parts))

        return gemini_history

    def format_functions_for_gemini(self, function_descriptions):
        """Format OpenAI tools format for Gemini's function calling API."""
        function_declarations = []
        try:
            functions_data = json.loads(function_descriptions)

            for func in functions_data:
                # Handle OpenAI tools format
                if isinstance(func, dict) and func.get("type") == "function":
                    func_def = func["function"]
                    function_declaration = {
                        "name": func_def["name"],
                        "description": func_def.get("description", ""),
                        "parameters": func_def.get("parameters", {"type": "object", "properties": {}})
                    }
                    # Gemini requires at least one property
                    if not function_declaration["parameters"].get("properties"):
                        function_declaration["parameters"]["properties"] = {
                            "_dummy": {
                                "type": "string",
                                "description": "This parameter is not used but is required by the API."
                            }
                        }
                    function_declarations.append(function_declaration)
                # Legacy format fallback
                elif isinstance(func, dict) and func.get("functionName"):
                    function_name = func.get("functionName")
                    description = func.get("description", "")
                    parameter_info = self._parse_function_description(description)

                    function_declaration = {
                        "name": function_name,
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    }

                    for param_name, param_info in parameter_info.items():
                        function_declaration["parameters"]["properties"][param_name] = {
                            "type": "string",
                            "description": param_info.get("description", "")
                        }
                        if param_info.get("required", True):
                            function_declaration["parameters"]["required"].append(param_name)

                    if not function_declaration["parameters"]["properties"]:
                        function_declaration["parameters"]["properties"]["_dummy"] = {
                            "type": "string",
                            "description": "This parameter is not used but is required by the API."
                        }

                    function_declarations.append(function_declaration)

        except json.JSONDecodeError as e:
            print(f"Error parsing function descriptions: {e}")
        except Exception as e:
            print(f"Error formatting functions: {e}")

        return function_declarations

    def _parse_function_description(self, description):
        """Extract parameter information from function docstring."""
        parameters = {}

        if "Parameters:" in description:
            params_section = description.split("Parameters:")[1].split("Returns:")[0]
            param_lines = params_section.strip().split("\n")

            for line in param_lines:
                line = line.strip()
                if line and ":" in line:
                    parts = line.split(":", 1)
                    param_name = parts[0].strip()
                    param_desc = parts[1].strip() if len(parts) > 1 else ""

                    is_required = "optional" not in param_desc.lower()

                    parameters[param_name] = {
                        "description": param_desc,
                        "required": is_required
                    }

        return parameters

    def _convert_gemini_fc_to_tool_calls(self, response) -> list:
        """Convert Gemini function_calls response to OpenAI tool_calls format."""
        if not response.candidates or not response.candidates[0].content:
            return None
        if not hasattr(response.candidates[0].content, 'function_calls'):
            return None

        function_calls = response.candidates[0].content.function_calls
        if not function_calls:
            return None

        tool_calls = []
        for i, fc in enumerate(function_calls):
            args = type(fc.args).to_dict(fc.args) if fc.args else {}
            tool_calls.append({
                "id": f"call_gemini_{int(time.time())}_{i}",
                "type": "function",
                "function": {
                    "name": fc.name,
                    "arguments": json.dumps(args)
                }
            })
            log_message_flow("GeminiService", "FunctionRegistry", "tool_call", None, fc.name)

        return tool_calls or None

    def send_message(self, message_data: dict, conversation_history: list, system_prompt: str, functions_list: str = None,
                     skip_session: bool = False, is_group: bool = False,
                     profile=None) -> dict:
        """Process message, call Gemini API and return response."""
        sender_id = message_data.get('senderInboxId', 'unknown')
        content_preview = truncate_message(message_data.get('content'))
        log_message_flow("ChatController", "GeminiService", "send_message", sender_id, content_preview)
        conversation_id = message_data.get("conversationId")
        timestamp = int(time.time())

        if not self.client:
            return self._create_error_response("Gemini client not initialized.", conversation_id, timestamp)
        if not conversation_id:
            log_message_flow("GeminiService", "SYSTEM", "error", None, "Missing conversationId")
            return self._create_error_response("Internal Error: Missing conversation context.", conversation_id, timestamp)

        log_message_flow("GeminiService", "SYSTEM", "processing", conversation_id, "preparing request")

        try:
            gemini_history = self.translate_history_to_gemini_format([] if skip_session else conversation_history)

            content = message_data.get("content")
            current_parts = []
            content_type = message_data.get("contentType")

            # Handle role: "tool" from function returns
            is_tool_return = message_data.get("role") == "tool"
            if is_tool_return:
                tool_content = content
                if isinstance(tool_content, dict):
                    tool_content = tool_content.get("value", str(tool_content))
                current_parts.append(types.Part.from_text(text=f"Function returned: {tool_content}"))

            # Handle attachment content type for the current message
            elif content_type == 'attachment':
                mime_type = message_data.get("mimeType")
                image_data = content.get("data") if isinstance(content, dict) else None
                text_value = content.get("value") if isinstance(content, dict) else None

                if mime_type and image_data and isinstance(image_data, list):
                    try:
                        image_bytes = bytes(image_data)
                        current_parts.append(types.Part.from_bytes(mime_type=mime_type, data=image_bytes))
                    except Exception as e:
                        print(f"Warning: Could not process image data for current message: {e}")

                if text_value:
                    current_parts.append(types.Part.from_text(text=str(text_value)))
            elif not is_tool_return:
                if isinstance(content, dict):
                    if content.get("type") == "functionReturn":
                        value = content.get("value", "No result provided")
                        current_parts.append(types.Part.from_text(text=f"Function returned: {value}"))
                    elif content.get("value"):
                        current_parts.append(types.Part.from_text(text=str(content.get("value"))))
                elif isinstance(content, str):
                    current_parts.append(types.Part.from_text(text=content))
                elif content:
                    current_parts.append(types.Part.from_text(text=str(content)))

            if not current_parts:
                current_parts.append(types.Part.from_text(text="..."))

            function_declarations = []
            if functions_list:
                function_declarations = self.format_functions_for_gemini(functions_list)

            tools = None
            if function_declarations:
                tools = [types.Tool(function_declarations=function_declarations)]

            generate_content_config = types.GenerateContentConfig(
                response_mime_type="text/plain",
                system_instruction=[types.Part.from_text(text=system_prompt)]
            )

            if tools:
                generate_content_config.tools = tools

            contents = gemini_history + [types.Content(role="user", parts=current_parts)]

            log_message_flow("GeminiService", "Gemini_API", "api_request", conversation_id, "sending message")
            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=generate_content_config,
            )
            log_message_flow("Gemini_API", "GeminiService", "api_response", conversation_id, "received response")

            raw_text = ""
            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'text') and part.text:
                        raw_text += part.text

            # Convert Gemini function calls to standardized tool_calls format
            tool_calls = self._convert_gemini_fc_to_tool_calls(response)

            token_count = 0
            usage_metadata = getattr(response, 'usage_metadata', None)
            if usage_metadata and hasattr(usage_metadata, 'total_token_count'):
                token_count = usage_metadata.total_token_count

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
                    "content": parsed_response.get("content", {"type": "text", "value": "I couldn't process your request properly."}),
                    "timestamp": timestamp,
                    "conversationId": conversation_id,
                    "tool_calls": tool_calls,
                    "token_count": token_count
                }

            except json.JSONDecodeError as e:
                print(f"Error parsing JSON response: {e}. Raw text: {raw_text}")
                return {
                    "role": "assistant",
                    "content": {"type": "text", "value": raw_text},
                    "timestamp": timestamp,
                    "conversationId": conversation_id,
                    "tool_calls": tool_calls,
                    "token_count": token_count
                }

        except Exception as e:
            log_message_flow("GeminiService", "SYSTEM", "error", conversation_id, str(e))
            return self._create_error_response(f"Service Error: {str(e)}", conversation_id, timestamp)

    def quick_call(self, prompt: str, max_tokens: int,
                   model: Optional[str] = None) -> Optional[str]:
        try:
            config = types.GenerateContentConfig(
                response_mime_type="text/plain",
                system_instruction=[types.Part.from_text(text="You are a helpful assistant. Be concise.")],
                temperature=0.3,
                max_output_tokens=max_tokens,
            )
            resp = self.client.models.generate_content(
                model=model or "gemini-flash-lite-latest",
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=config,
            )
            text = ""
            if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                for part in resp.candidates[0].content.parts:
                    if hasattr(part, 'text') and part.text:
                        text += part.text
            return text.strip() or None
        except Exception as e:
            logger.warning(f"[GeminiService.quick_call] {e}")
            return None
