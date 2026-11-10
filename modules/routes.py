"""Where each conversation goes: what the admin panel shows.

Every incoming message leaves one line here: the filter's verdict, the profile that took
it, the gate's decision and the session it resumes. Kept in memory (the last state per
conversation) and appended to <data_dir>/routes.jsonl, so the panel survives a restart.
`explain()` answers "who would handle this?" without sending anything.
"""
import json
import os
import threading
import time

from config import Config

_lock = threading.Lock()
_latest = {}            # conversation_id -> dict
_loaded = False
MAX_FILE_BYTES = 5 * 1024 * 1024


def _path():
    return os.path.join(Config.DATA_DIR, 'routes.jsonl')


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_path(), 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                prev = _latest.get(row.get('conversation_id'))
                row['count'] = (prev or {}).get('count', 0) + 1
                _latest[row.get('conversation_id')] = row
    except OSError:
        pass


def note(message, kind, profile, decision):
    """Record what happened to one incoming message."""
    conv = message.get('conversationId') or ''
    row = {
        'at': int(time.time()),
        'conversation_id': conv,
        'kind': kind or 'dm',
        'sender': message.get('senderInboxId') or '',
        'profile': getattr(profile, 'name', None),
        'gate': getattr(profile, 'gate', None),
        'workdir': getattr(profile, 'workdir', None),
        'session_key': profile.session_key(conv) if profile is not None else None,
        'decision': decision,
    }
    with _lock:
        _load()
        prev = _latest.get(conv)
        row['count'] = (prev or {}).get('count', 0) + 1
        _latest[conv] = row
        try:
            os.makedirs(Config.DATA_DIR, exist_ok=True)
            path = _path()
            if os.path.exists(path) and os.path.getsize(path) > MAX_FILE_BYTES:
                os.replace(path, path + '.1')         # keep one old file
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({k: v for k, v in row.items() if k != 'count'}) + '\n')
        except OSError:
            pass


def latest():
    """The last route of every conversation, newest first."""
    with _lock:
        _load()
        rows = list(_latest.values())
    return sorted(rows, key=lambda r: r.get('at', 0), reverse=True)


def explain(conversation_id, sender_inbox_id, kind='dm'):
    """Who would handle a message, without handling it."""
    from modules import filter as message_filter
    from modules.profiles import _from_config, _base, _match, owner_for, GROUP, DEFAULT
    verdict, rule = message_filter.decide(conversation_id, sender_inbox_id, kind)
    out = {'filter': verdict, 'filter_rule': rule}
    if verdict == 'ignore':
        return out
    is_group = kind == 'group'
    name = (_match(conversation_id) if is_group else None) or _match(sender_inbox_id)
    p = _from_config(name, Config.PROFILES[name]) if name else _base(GROUP if is_group else DEFAULT, is_group)
    if p.sessions == 'per_account':
        if is_group:
            p.sessions = 'per_conversation'
        else:
            p.session_owner = owner_for(sender_inbox_id)
    out.update({
        'profile': p.name, 'gate': p.gate, 'provider': p.provider or Config.LLM_SERVICE,
        'model': p.model, 'workdir': p.workdir, 'tools': p.allowed_tools if not p.no_tools else [],
        'privacy': p.privacy, 'sessions': p.sessions, 'session_key': p.session_key(conversation_id),
    })
    return out
