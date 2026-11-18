"""How often a bot may post in a group: `group_reply_secs` (the profile's, else
[server] group_reply_secs; 0 = no limit).

A bot in a group is still run for every turn it gets (a stack of new messages, a
timer fire): it can read, think and act with its tools. Only its answer is held
back when it posted in that group less than `group_reply_secs` ago. The turn's
text tells it which case it is in (note()), and a bot that has nothing to add
answers [NO_REPLY], which is never sent. The time of each group post is kept in
`<data_dir>/group_posts.json`, keyed by the chat (a group seen by one of several
inboxes is its own chat, modules/inboxes.py), so a restart does not reset it.
"""
import datetime
import json
import os
import threading
import time

from config import Config
from modules import profiles

_lock = threading.Lock()
_posts = None


def _path():
    return os.path.join(Config.DATA_DIR, 'group_posts.json')


def _load():
    global _posts
    if _posts is None:
        try:
            with open(_path(), 'r', encoding='utf-8') as f:
                _posts = {k: float(v) for k, v in dict(json.load(f)).items()}
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            _posts = {}
    return _posts


def every(conversation_id):
    return profiles.setting(conversation_id, 'group_reply_secs', getattr(Config, 'GROUP_REPLY_SECS', 0))


def next_allowed(conversation_id):
    """Epoch seconds from which this bot may post in this group again (0 = now)."""
    secs = every(conversation_id)
    if secs <= 0:
        return 0
    with _lock:
        last = _load().get(conversation_id)
    return last + secs if last else 0


def allowed(conversation_id):
    return next_allowed(conversation_id) <= time.time()


def mark(conversation_id):
    """This bot just posted in this group."""
    with _lock:
        _load()[conversation_id] = time.time()
        tmp = _path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_posts, f, indent=1, sort_keys=True)
        os.replace(tmp, _path())


def note(conversation_id):
    """The line added to a group turn: may it post now, and how to stay silent."""
    if allowed(conversation_id):
        return ("[Group chat. You may post here this turn. If you have nothing worth adding, "
                "answer exactly [NO_REPLY] and nothing is sent.]")
    until = datetime.datetime.fromtimestamp(next_allowed(conversation_id)).strftime('%H:%M')
    return (f"[Group chat. You may not post here until {until}: whatever you answer this turn is NOT sent. "
            "You can still act with your tools. Answer [NO_REPLY].]")
