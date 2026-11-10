"""
Utility module for standardized message flow logging across gpette.
This helps track message flow between different modules for debugging and monitoring.
"""

def log_message_flow(sender_module, receiver_module, message_type, sender_id=None, preview=None):
    """
    Log message flow in a standardized format for easy tracking.
    
    Args:
        sender_module (str): The module/service sending the message
        receiver_module (str): The module/service receiving the message  
        message_type (str): Type of message (e.g., 'user_message', 'api_request', 'error')
        sender_id (str, optional): ID of the sender (e.g., user inbox ID, conversation ID)
        preview (str, optional): Preview of the message content (will be truncated)
    
    Example output:
        [MSG] CLIENT -> ChatController[user123] | user_message | Hello, how are you today?
        [MSG] GeminiService -> Gemini_API[conv456] | api_request | sending message
        [MSG] FunctionRegistry -> SYSTEM | error | param extraction failed: get_weather
    """
    sender_info = f"[{sender_id}]" if sender_id else ""
    preview_text = f" | {str(preview)[:50]}..." if preview and len(str(preview)) > 50 else f" | {preview}" if preview else ""
    print(f"[MSG] {sender_module} -> {receiver_module}{sender_info} | {message_type}{preview_text}")

def truncate_message(content, max_length=50):
    """
    Helper function to truncate message content for logging.
    
    Args:
        content: The content to truncate (can be string, dict, etc.)
        max_length (int): Maximum length before truncation
    
    Returns:
        str: Truncated string representation of the content
    """
    if not content:
        return "None"
    content_str = str(content)
    return content_str[:max_length] + "..." if len(content_str) > max_length else content_str