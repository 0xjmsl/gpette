"""Transcripts: each person's conversation written as plain files the bot can read back.

With `transcripts = true` on a profile (or `[claude_code] transcripts = true`), every user
and assistant message is appended to

    <profile workdir>/conversations/<owner>/<YYYY-MM>.md

where <owner> is the session owner (`per_account`: the person's account, else their inbox)
or the conversation for groups. A bot with only Read / Glob / Grep can then search what a
person said weeks ago, after a session reset, without any write tool.

Each run is fenced to its own folder: the Claude Code service adds deny rules for every
OTHER folder under conversations/ (deny wins over allow), so one person's bot can't read
another person's transcript. `tests/scenarios/transcripts.toml` checks it.
"""
import logging
import os
import re
import threading
import time

FOLDER = 'conversations'
_lock = threading.Lock()


def _safe(name):
    name = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(name or '')).strip('._')
    return name[:80] or 'unknown'


def owner_folder_name(profile, conversation_id, kind=None):
    if getattr(profile, 'session_owner', ''):
        return _safe(profile.session_owner)
    prefix = 'group-' if kind == 'group' or getattr(profile, 'per_group_workdir', False) else 'conv-'
    return _safe(prefix + str(conversation_id))


def base_dir(profile, conversation_id):
    from modules.profiles import workdir_for
    root = workdir_for(profile, conversation_id)
    return os.path.join(root, FOLDER) if root else ''


def _text(message):
    content = message.get('content')
    if isinstance(content, dict):
        if content.get('type') == 'attachment':
            name = content.get('filename') or 'a file'
            caption = (content.get('description') or '').strip()
            return f"[sent {name}]" + (f" {caption}" if caption else '')
        value = content.get('value')
        return value if isinstance(value, str) else ''
    return content if isinstance(content, str) else ''


def record(message, kind=None):
    """Append one stored message to its owner's transcript, if the profile wants it."""
    role = message.get('role')
    if role not in ('user', 'assistant'):
        return
    conv = message.get('conversationId')
    text = _text(message).strip()
    if not conv or not text or text.startswith('Timer fired:'):
        return
    try:
        from modules.profiles import profile_for_conversation
        profile = profile_for_conversation(conv, kind)
        if not getattr(profile, 'transcripts', False):
            return
        base = base_dir(profile, conv)
        if not base:
            return
        folder = os.path.join(base, owner_folder_name(profile, conv, kind))
        ts = message.get('timestamp') or time.time() * 1000
        ts = ts / 1000 if ts > 1e11 else ts
        stamp = time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))
        who = 'you (the bot)' if role == 'assistant' else (message.get('senderName') or 'them')
        with _lock:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, time.strftime('%Y-%m', time.gmtime(ts)) + '.md')
            with open(path, 'a', encoding='utf-8') as f:
                f.write(f"\n## {stamp} UTC · {who}\n\n{text}\n")
    except Exception as e:
        logging.error(f"transcripts: could not record a message in {conv}: {e}")


def denials(profile, conversation_id, kind=None):
    """Deny rules hiding every other person's transcript folder from this run."""
    if not getattr(profile, 'transcripts', False):
        return []
    base = base_dir(profile, conversation_id)
    own = owner_folder_name(profile, conversation_id, kind)
    try:
        others = [d for d in os.listdir(base) if d != own and os.path.isdir(os.path.join(base, d))]
    except OSError:
        return []
    rules = []
    for d in others:
        for tool in ('Read', 'Glob', 'Grep'):
            rules += [f"{tool}(./{FOLDER}/{d})", f"{tool}(./{FOLDER}/{d}/**)"]
    return rules
