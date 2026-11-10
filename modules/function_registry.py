import json
import datetime
import importlib.util
import os
import ast
import inspect

from utils.message_logger import log_message_flow

# Every top-level .py in ./tools whose name does not start with "sys_" is a tool.
# The function inside must have the file's name; its docstring is the description.
TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


class FunctionRegistry:
    FUNCTIONS_DIR = TOOLS_DIR

    def __init__(self, thread_controller=None):
        self.thread_controller = thread_controller
        self.functions = {}
        self.function_descriptions = {}

    def get_available_functions(self):
        return [
            filename[:-3]
            for filename in os.listdir(self.FUNCTIONS_DIR)
            if filename.endswith(".py") and not filename.startswith(("sys_", "_"))
        ]

    def load_function(self, function_name):
        file_path = os.path.join(self.FUNCTIONS_DIR, f"{function_name}.py")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"No file found for function: {function_name}")

        spec = importlib.util.spec_from_file_location(function_name, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if hasattr(module, function_name):
            return getattr(module, function_name)
        else:
            raise AttributeError(
                f"Function '{function_name}' not found in file {file_path}"
            )

    def get_function_description(self, function_name):
        file_path = os.path.join(self.FUNCTIONS_DIR, f"{function_name}.py")

        with open(file_path, "r", encoding="utf-8") as file:
            tree = ast.parse(file.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ):
                    return node.body[0].value.value.strip()

        return "No description available."

    def execute_function(self, function_name, parameters, conversation_id=None):
        available_functions = self.get_available_functions()

        # A profile with a `tools` list may only run those (the LLM was only
        # offered those; this stops a made-up call too).
        if conversation_id and function_name in available_functions:
            from modules.profiles import profile_for_conversation
            from models.conversation_db import ConversationDB
            if not profile_for_conversation(conversation_id, None, ConversationDB()).tool_allowed(function_name):
                available_functions = []

        if function_name not in available_functions:
            return {
                "role": "system",
                'address': self.thread_controller.get_address(),
                "timestamp": self.get_current_timestamp(),
                "content": {"type": "functionReturn", "value": "Not a Known Function."},
                "source": "FunctionRegistry",
                "conversationId": "",
            }
        try:
            self.functions[function_name] = self.load_function(function_name)

            # Inject _conversation_id if the function accepts it
            if conversation_id:
                sig = inspect.signature(self.functions[function_name])
                if '_conversation_id' in sig.parameters:
                    parameters['_conversation_id'] = conversation_id

            result = self.functions[function_name](**parameters)

        except Exception as e:
            return {
                "role": "system",
                'address': self.thread_controller.get_address(),
                "timestamp": self.get_current_timestamp(),
                "content": {
                    "type": "functionReturn",
                    "value": f"Error executing the function: {str(e)}",
                },
                "source": "FunctionRegistry"
            }

        return {
            "role": "system",
            'address': self.thread_controller.get_address(),
            "timestamp": self.get_current_timestamp(),
            "content": {"type": "functionReturn", "value": result},
            "source": "FunctionRegistry"
        }

    def get_current_timestamp(self):
        now = datetime.datetime.now()
        timestamp = int(now.timestamp() * 1000)
        return timestamp

    def list_available_functions(self):
        return self.get_available_functions()

    def get_functions_with_descriptions(self):
        functions = []
        for function_name in self.get_available_functions():
            functions.append(
                {
                    "functionName": function_name,
                    "description": self.get_function_description(function_name),
                }
            )
        return json.dumps(functions)

    def get_function_parameters(self, function_name):
        """Extract parameter information from a function."""
        try:
            # Load the function
            function = self.load_function(function_name)
            
            # Get the docstring to parse for parameter descriptions
            docstring = function.__doc__ or ""
            
            # Get parameter names from inspect
            sig = inspect.signature(function)
            params = {}
            
            for name, param in sig.parameters.items():
                # Skip internal params (injected at runtime, not exposed to LLM)
                if name.startswith('_'):
                    continue
                has_default = param.default != inspect.Parameter.empty
                params[name] = {
                    "required": not has_default,
                    "description": "",  # Will be filled from docstring
                    "default": param.default if has_default else None
                }
            
            # Parse docstring for parameter descriptions
            if "Parameters:" in docstring:
                param_section = docstring.split("Parameters:")[1]
                if "Returns:" in param_section:
                    param_section = param_section.split("Returns:")[0]
                
                param_lines = param_section.strip().split("\n")
                current_param = None
                
                for line in param_lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Check if this is a parameter definition line
                    param_match = line.split("(", 1)[0].strip() if "(" in line else line
                    
                    if param_match in params:
                        current_param = param_match
                        desc = line.split(":", 1)[1].strip() if ":" in line else ""
                        params[current_param]["description"] = desc
                    elif current_param and line:
                        # This is a continuation of the previous parameter description
                        params[current_param]["description"] += " " + line
            
            return params
            
        except Exception as e:
            log_message_flow("FunctionRegistry", "SYSTEM", "error", None, f"param extraction failed: {function_name}")
            return {}

    def get_function_declarations_for_tools(self):
        """Get function declarations in OpenAI tools format."""
        tools = []

        for function_name in self.get_available_functions():
            try:
                # Get basic function info
                description = self.get_function_description(function_name)
                param_info = self.get_function_parameters(function_name)

                # Create OpenAI-compatible tool declaration
                tool = {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    }
                }

                # Add parameters
                for param_name, param_data in param_info.items():
                    # Skip internal/injected parameters
                    if param_name == "api_manager" or param_name.startswith('_'):
                        continue

                    tool["function"]["parameters"]["properties"][param_name] = {
                        "type": "string",
                        "description": param_data.get("description", "")
                    }

                    if param_data.get("required", True):
                        tool["function"]["parameters"]["required"].append(param_name)

                # If no parameters were added, ensure we have empty properties (LMStudio requires this)
                if not tool["function"]["parameters"]["properties"]:
                    tool["function"]["parameters"] = {
                        "type": "object",
                        "properties": {}
                    }

                tools.append(tool)

            except Exception as e:
                # Log error but continue processing other functions
                log_message_flow("FunctionRegistry", "SYSTEM", "error", None, f"Failed to load function '{function_name}': {str(e)}")
                continue

        return tools

    def receive_Message_FunctionRegistryr(self, message: dict):
        conversationId = message.get("conversationId")

        if message.get("tool_calls"):
            tool_calls = message["tool_calls"]
            if isinstance(tool_calls, str):
                try:
                    tool_calls = json.loads(tool_calls)
                except:
                    log_message_flow("FunctionRegistry", "SYSTEM", "error", None, "Failed to parse tool_calls")
                    return
            
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    function_info = tool_call.get("function", {})
                    function_name = function_info.get("name")
                    tool_call_id = tool_call.get("id")
                    
                    if function_name == "reset_chat" or function_name == "dontReply":
                        continue
                    
                    # Parse arguments (they come as JSON string in OpenAI format)
                    try:
                        arguments = function_info.get("arguments", "{}")
                        if isinstance(arguments, str):
                            parameters = json.loads(arguments)
                        else:
                            parameters = arguments
                    except json.JSONDecodeError:
                        parameters = {}
                        log_message_flow("FunctionRegistry", "SYSTEM", "error", None, f"Failed to parse arguments for {function_name}")
                    
                    # Execute function and create tool response
                    result = self.execute_function(
                        function_name, parameters, conversation_id=conversationId
                    )

                    # Convert to tool response format
                    tool_result = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result["content"]["value"] if isinstance(result.get("content"), dict) else str(result.get("content", "")),
                        "timestamp": result["timestamp"],
                        "source": "FunctionRegistry",
                        "conversationId": conversationId
                    }

                    self.thread_controller.add_message(tool_result)