import time as _time
from datetime import datetime, timedelta
from models.conversation_db import ConversationDB

def _parse_time_value(time_str):
    """
    Parse a time value that could be either:
    - A plain number → minutes from now (e.g. "30")
    - A clock time → absolute time (e.g. "19:30", "7:30 PM", "7PM")

    Returns (minutes_from_now, label_str, is_clock_time) or (None, error_msg, False).
    """
    time_str = time_str.strip()

    # Check if it's a plain number (relative minutes)
    try:
        minutes_val = int(float(time_str))
        if minutes_val < 1:
            return None, "Timer must be at least 1 minute.", False
        return minutes_val, f"in {minutes_val}min", False
    except (ValueError, TypeError):
        pass

    # Otherwise try to parse as an absolute clock time
    now = datetime.now()
    formats = [
        "%H:%M",       # 19:30
        "%I:%M %p",    # 7:30 PM
        "%I:%M%p",     # 7:30PM
        "%I %p",       # 7 PM
        "%I%p",        # 7PM
    ]

    parsed_time = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(time_str.upper(), fmt)
            parsed_time = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            break
        except ValueError:
            continue

    if parsed_time is None:
        return None, f"Could not parse '{time_str}'. Use a clock time like '19:30' or '7:30 PM', or a number of minutes like '30'.", False

    # If time already passed today, schedule for tomorrow
    if parsed_time <= now:
        parsed_time += timedelta(days=1)
        day_note = " (tomorrow)"
    else:
        day_note = ""

    delta_seconds = (parsed_time - now).total_seconds()
    minutes_from_now = max(1, int(delta_seconds / 60))

    formatted = parsed_time.strftime("%H:%M") + day_note
    return minutes_from_now, f"at {formatted}", True


def setTimer(description, instructions, time="", timer_type="one_shot", _conversation_id=None):
    """
    Set a timer/reminder that fires at a specific time or after a delay.

    USE THIS WHEN:
    - The user asks you to remind them about something later
    - You need to schedule a follow-up action
    - The user wants a recurring check-in on a topic

    Parameters:
    description (str): Short label for the timer (e.g. "Laundry reminder")
    instructions (str): What you should do when the timer fires. Be specific — this is what you'll see later (e.g. "Remind the user to switch laundry to the dryer")
    time (str): When the timer should fire. Pass EXACTLY what the user said:
        - A clock time like "19:30" or "7:30 PM" (for specific times)
        - A number of minutes like "30" (for relative delays like "in 30 minutes")
        Examples: "19:30", "7:30 PM", "8 PM", "30", "90"
    timer_type (str): "one_shot" (fires once) or "recurring" (repeats). Default: "one_shot"
        - With a clock time (e.g. "8 AM"): recurring = fires daily at that time
        - With relative minutes (e.g. "30"): recurring = repeats every N minutes

    Returns:
    str: Confirmation with the timer ID and scheduled time
    """
    if not _conversation_id:
        return "Error: Could not determine conversation context."


    if not time:
        return "Error: Provide a time value (e.g. '19:30' or '30' for 30 minutes)."

    minutes_val, info, is_clock_time = _parse_time_value(time)
    if minutes_val is None:
        return f"Error: {info}"

    fire_at = int(_time.time()) + (minutes_val * 60)
    if timer_type == "recurring":
        # Clock time → repeat daily (24h). Relative minutes → repeat at that interval.
        interval_seconds = 86400 if is_clock_time else (minutes_val * 60)
    else:
        interval_seconds = None

    db = ConversationDB()
    timer_id = db.create_timer(
        conversation_id=_conversation_id,
        description=description,
        instructions=instructions,
        fire_at=fire_at,
        timer_type=timer_type,
        interval_seconds=interval_seconds
    )

    type_note = " (recurring)" if timer_type == "recurring" else ""
    return f"Timer '{description}' set ({info}{type_note}). ID: {timer_id}"
