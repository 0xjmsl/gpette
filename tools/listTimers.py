import time
from models.conversation_db import ConversationDB

def listTimers(_conversation_id=None):
    """
    List all active timers for the current conversation.

    USE THIS WHEN:
    - The user asks what timers or reminders are set
    - You need to check existing timers before creating a new one

    Returns:
    str: Formatted list of active timers with IDs, descriptions, and fire times
    """
    if not _conversation_id:
        return "Error: Could not determine conversation context."

    db = ConversationDB()
    timers = db.get_timers_for_conversation(_conversation_id)

    if not timers:
        return "No active timers."

    now = int(time.time())
    lines = []
    for t in timers:
        fire_at = t.get('fire_at', 0)
        remaining = fire_at - now
        if remaining > 0:
            mins = remaining // 60
            secs = remaining % 60
            time_str = f"in {mins}m {secs}s"
        else:
            time_str = "overdue"

        type_label = ""
        if t.get('timer_type') == 'recurring':
            interval = t.get('interval_seconds', 0)
            if interval == 86400:
                type_label = " (recurring daily)"
            else:
                type_label = f" (recurring every {interval // 60}min)"

        lines.append(f"- [{t['timer_id']}] {t['description']}: fires {time_str}{type_label}")

    return "\n".join(lines)
