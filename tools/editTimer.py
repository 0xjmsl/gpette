import time as _time
from models.conversation_db import ConversationDB
from tools.setTimer import _parse_time_value

def editTimer(timer_id, description=None, instructions=None, time=None, timer_type=None):
    """
    Edit an existing active timer.

    USE THIS WHEN:
    - The user wants to change a reminder's timing, description, or instructions
    - You need to adjust a timer that was already set

    Parameters:
    timer_id (str): The ID of the timer to edit
    description (str): New description (optional)
    instructions (str): New instructions for when it fires (optional)
    time (str): New time for the timer (optional, resets fire time). Same format as setTimer:
        - A clock time like "19:30" or "7:30 PM"
        - A number of minutes like "30"
    timer_type (str): Change to "one_shot" or "recurring" (optional)

    Returns:
    str: Confirmation of the update
    """
    db = ConversationDB()
    timer = db.get_timer_by_id(timer_id)

    if not timer:
        return f"Error: Timer '{timer_id}' not found or not active."

    if timer.get('status') != 'active':
        return f"Error: Timer '{timer_id}' is {timer.get('status')}, cannot edit."

    kwargs = {}
    if description is not None:
        kwargs['description'] = description
    if instructions is not None:
        kwargs['instructions'] = instructions
    if time is not None:
        minutes_val, info, is_clock_time = _parse_time_value(time)
        if minutes_val is None:
            return f"Error: {info}"
        kwargs['fire_at'] = int(_time.time()) + (minutes_val * 60)
        if timer_type == 'recurring' or (timer_type is None and timer.get('timer_type') == 'recurring'):
            kwargs['interval_seconds'] = 86400 if is_clock_time else (minutes_val * 60)
    if timer_type is not None:
        kwargs['timer_type'] = timer_type

    if not kwargs:
        return "Nothing to update — provide at least one field to change."

    db.update_timer(timer_id, **kwargs)
    return f"Timer '{timer_id}' updated ({info if time else 'details changed'})."
