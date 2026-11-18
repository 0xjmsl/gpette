"""Several inboxes, one gpette.

By default gpette talks to ONE xmtp_daemon (`[daemon] url`), so every chat is
with the same XMTP inbox. To give several bots their own inbox (their own name
and picture in the chat list), run one daemon per inbox and list them:

    [inboxes.mapmaker]
    url = "http://127.0.0.1:3111"

    [profiles.mapmaker]
    inbox = "mapmaker"            # this profile speaks through that inbox

Routing then follows two plain rules:

- A message that ARRIVES: the daemon says which inbox received it (the
  envelope's `inboxId`), so only the profiles bound to that inbox are tried
  (group id, then sender; a bound profile with no `members` answers everyone
  who writes to its inbox). Profiles with no `inbox` are tried after them.
- A message that LEAVES goes out through the inbox its conversation belongs to.
  gpette learns that from the first message it receives there, or from the
  profile of a timer that fires there, and keeps it in
  `<data_dir>/conversation_inboxes.json` across restarts. A conversation it
  knows nothing about goes out through `[daemon] url`.

A GROUP has one conversation id for all its members, so two of these inboxes
in the same group would share one chat inside gpette. To keep them apart, a
group message that arrives at a configured inbox gets the conversation id
`<group id>@<inbox name>`: each bot has its own history, session, profile and
timers in the group, and a message to that id leaves through that inbox with
the plain group id. DMs need nothing: a DM belongs to one inbox already.

Each daemon's inbox id is read from its `/health`, so the config never holds
an inbox id. A config with no `[inboxes]` works exactly as before.
"""
import json
import logging
import os
import threading

import requests

from config import Config

_lock = threading.Lock()
_conv_inbox = None          # conversation id -> inbox NAME
_ids = {}                   # inbox name -> inbox id (from /health)


def configured():
    return bool(Config.INBOXES)


def url_of(name):
    entry = Config.INBOXES.get(name)
    return str(entry.get('url') or '').rstrip('/') if isinstance(entry, dict) else ''


def _norm(value):
    return str(value or '').strip().lower().removeprefix('0x')


def inbox_id_of(name, refresh=False):
    """The inbox id behind a configured inbox, read from its daemon's /health
    (cached). '' when the daemon does not answer."""
    if not refresh and _ids.get(name):
        return _ids[name]
    url = url_of(name)
    if not url:
        return ''
    try:
        r = requests.get(f"{url}/health", timeout=5)
        r.raise_for_status()
        inbox = _norm(r.json().get('inboxId'))
    except Exception as e:
        logging.warning(f"inboxes: '{name}' ({url}) did not answer /health: {e}")
        return ''
    if inbox:
        _ids[name] = inbox
    return inbox


def name_of(inbox_id):
    """The configured name of the inbox with this id, or None."""
    key = _norm(inbox_id)
    if not key or not configured():
        return None
    for name in Config.INBOXES:
        if _ids.get(name) == key:
            return name
    for name in Config.INBOXES:            # not cached yet (a daemon was down at boot)
        if inbox_id_of(name, refresh=True) == key:
            return name
    logging.warning(f"inboxes: a message arrived at inbox {key[:12]}..., which is not in [inboxes]")
    return None


def _path():
    return os.path.join(Config.DATA_DIR, 'conversation_inboxes.json')


def _load():
    global _conv_inbox
    if _conv_inbox is None:
        try:
            with open(_path(), 'r', encoding='utf-8') as f:
                _conv_inbox = dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            _conv_inbox = {}
    return _conv_inbox


def _save():
    tmp = _path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_conv_inbox, f, indent=1, sort_keys=True)
    os.replace(tmp, _path())


def note(conversation_id, name):
    """Remember that a conversation belongs to inbox `name` (a configured name)."""
    if not conversation_id or not name or name not in Config.INBOXES:
        return
    with _lock:
        known = _load()
        if known.get(conversation_id) == name:
            return
        if conversation_id in known:
            logging.warning(f"inboxes: conversation {conversation_id[:12]}... moved from "
                            f"'{known[conversation_id]}' to '{name}'")
        known[conversation_id] = name
        _save()


def of_conversation(conversation_id):
    """The inbox name a conversation belongs to, or None when unknown."""
    if not conversation_id:
        return None
    with _lock:
        return _load().get(conversation_id)


def split(conversation_id):
    """(XMTP conversation id, inbox name or None): `<group>@<inbox>` -> its parts."""
    conv = str(conversation_id or '')
    if '@' in conv:
        base, name = conv.rsplit('@', 1)
        if name in (Config.INBOXES or {}):
            return base, name
    return conv, None


def base_conversation(conversation_id):
    """The XMTP conversation id (a group's own id, without `@<inbox>`)."""
    return split(conversation_id)[0]


def group_key(group_id, name):
    """The conversation id gpette uses for a group as seen by inbox `name`."""
    return f"{group_id}@{name}"


def route(conversation_id):
    """(daemon url, XMTP conversation id) for a message to this conversation."""
    base, name = split(conversation_id)
    if not name and configured():
        name = of_conversation(conversation_id)
    return (url_of(name) if name else Config.XMTP_DAEMON_URL), base


def daemon_url_for(conversation_id):
    """Where a message to this conversation is sent."""
    return route(conversation_id)[0]


def check_config():
    """Refuse to start with a profile bound to an inbox that is not configured,
    or an inbox with no url. Returns nothing; raises ValueError."""
    for name, entry in (Config.INBOXES or {}).items():
        if not isinstance(entry, dict) or not str(entry.get('url') or '').strip():
            raise ValueError(f"[inboxes.{name}] needs a url (its xmtp_daemon, e.g. http://127.0.0.1:3111)")
    for pname, raw in (Config.PROFILES or {}).items():
        inbox = str(raw.get('inbox') or '').strip()
        if inbox and inbox not in (Config.INBOXES or {}):
            raise ValueError(f"[profiles.{pname}] inbox = \"{inbox}\", but there is no [inboxes.{inbox}]")


def status():
    """For the admin page: each inbox, its url and its inbox id (if it answered)."""
    return {name: {'url': url_of(name), 'inboxId': _ids.get(name, '')}
            for name in (Config.INBOXES or {})}
