from models.conversation_db import ConversationDB


def resetChat(_conversation_id=None):
    """
    Reset the chat conversation when the user requests it.

    Parameters:
        None

    Returns:
        str: Success message indicating the chat has been reset
    """
    if _conversation_id:
        db = ConversationDB()
        db.reset_conversation(_conversation_id)
        return "Chat has been reset"
    return "Chat has been reset"
