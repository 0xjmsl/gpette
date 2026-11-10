from models.conversation_db import ConversationDB

def cancelTimer(timer_id):
    """
    Cancel an active timer so it won't fire.

    USE THIS WHEN:
    - The user no longer needs a reminder
    - A timer should be stopped before it fires

    Parameters:
    timer_id (str): The ID of the timer to cancel

    Returns:
    str: Confirmation that the timer was cancelled
    """
    db = ConversationDB()
    timer = db.get_timer_by_id(timer_id)

    if not timer:
        return f"Error: Timer '{timer_id}' not found."

    if timer.get('status') != 'active':
        return f"Timer '{timer_id}' is already {timer.get('status')}."

    db.cancel_timer(timer_id)
    return f"Timer '{timer_id}' ({timer.get('description', '')}) cancelled."
