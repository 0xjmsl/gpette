"""Thread joining — group conversation IDs so they share history.

Each screen/inbox keeps its own conversation ID for storage and routing.
History loading queries all conversation IDs in the group via IN(...).

Config format:  THREAD_JOINS = [["conv-A", "conv-B"], ["conv-C", "conv-D"]]

Disable with THREAD_JOINS_ENABLED = False — get_group returns [input].
"""

from config import Config


def get_group(conversation_id: str) -> list:
    """Return all conversation IDs that share history with this one."""
    if not Config.THREAD_JOINS_ENABLED or not conversation_id:
        return [conversation_id]
    for group in Config.THREAD_JOINS:
        if conversation_id in group:
            return list(group)
    return [conversation_id]
