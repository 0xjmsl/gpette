"""`[SEND_XMTP_MESSAGE]`: a bot writes to another inbox, or to a registered group,
in the middle of its turn.

The model writes the block in its answer:

    [SEND_XMTP_MESSAGE]
    {INBOXID: <inbox id>,
    MESSAGE: the text to send}

(`GROUPID:` instead of `INBOXID:` for a group; a JSON body works too.) gpette
catches it, sends the message through the daemon, and gives the turn back with a
result line, the way a tool does: the run resumes its session with only that
result and carries on.

One block per answer, and the block is the whole point of that answer: any other
text in it is dropped, never delivered to the chat (the turn is not over). Two
blocks in one answer send nothing and return an error: one at a time, each with
its own result. The answer that ends the turn is the one with no block.

Three things bound it:

- `send = true` on the profile. Off by default; gpette never turns it on.
- A budget per turn. A turn starts from a natural prompt (a timer fire, or one
  or more stacked incoming messages) and gets `[send] per_item` sends for each
  item, `[send] max_per_turn` at most. A result is not a natural prompt and
  adds nothing. Only a message that was sent uses one up. Every result says how
  many are left.
- A group must be registered: its id is in some profile's `members`.

A result re-enters the queue as a non-user message, so the thread controller's
count of messages without a person applies to these round trips too.
"""
import json
import logging
import re
import threading
import time

from config import Config
from modules import inboxes

TOKEN = "[SEND_XMTP_MESSAGE]"
SOURCE = "SendBlock"                      # the `source` of a result message in the queue

# The token counts only when it is alone on its line (markdown marks around it are fine):
# an answer that names the block inside a sentence is talk, not a send (seen live
# 2026-11-20: an explanation that quoted the tag was held as a block with no body).
_TOKEN_RE = re.compile(r"^[ \t>*`]*\[\s*SEND[_ ]XMTP[_ ]MESSAGE\s*\][ \t*`]*$",
                       re.IGNORECASE | re.MULTILINE)
_TARGET_RE = re.compile(r"""^\s*["']?(INBOX_?ID|GROUP_?ID)["']?\s*:\s*["']?([^\s"',}]+)["']?\s*,?\s*""",
                        re.IGNORECASE)
_MESSAGE_RE = re.compile(r"""^\s*["']?MESSAGE["']?\s*:\s*""", re.IGNORECASE)
_INBOX_RE = re.compile(r"^[0-9a-f]{64}$")

_lock = threading.Lock()
_left = {}                                # the turn's conversation id -> sends left


# --- reading the block ---------------------------------------------------------------

def has_block(text):
    return isinstance(text, str) and bool(_TOKEN_RE.search(text))


def _from_json(body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    low = {str(k).strip().lower().replace('_', ''): v for k, v in data.items()}
    message = low.get('message')
    if not isinstance(message, str):
        return None
    if isinstance(low.get('inboxid'), str):
        return {'kind': 'inbox', 'target': low['inboxid'].strip(), 'message': message.strip()}
    if isinstance(low.get('groupid'), str):
        return {'kind': 'group', 'target': low['groupid'].strip(), 'message': message.strip()}
    return None


def _one(chunk):
    """One block's body (the text after its token, up to the next token)."""
    start = chunk.find('{')
    end = chunk.rfind('}')
    if start < 0 or end <= start:
        return {'error': 'the block has no {…} body'}
    parsed = _from_json(chunk[start:end + 1])
    if parsed:
        return parsed
    inner = chunk[start + 1:end]
    m = _TARGET_RE.match(inner)
    if not m:
        return {'error': 'the block needs INBOXID: or GROUPID: first'}
    kind = 'group' if m.group(1).upper().startswith('GROUP') else 'inbox'
    rest = inner[m.end():]
    mm = _MESSAGE_RE.match(rest)
    if not mm:
        return {'error': 'the block needs MESSAGE: after the id'}
    message = rest[mm.end():].strip()
    if len(message) >= 2 and message[0] == message[-1] and message[0] in "\"'":
        message = message[1:-1].strip()
    if not message:
        return {'error': 'the MESSAGE is empty'}
    return {'kind': kind, 'target': m.group(2).strip(), 'message': message}


def parse(text):
    """Every block in an answer, in order: a list of {kind, target, message} or
    {error}. Empty when the answer holds no block."""
    if not has_block(text):
        return []
    parts = _TOKEN_RE.split(text)[1:]         # what follows each token
    return [_one(p) for p in parts]


def strip(text):
    """The answer without its blocks (for a profile and engine that cannot take a result)."""
    if not has_block(text):
        return text
    return _TOKEN_RE.split(text)[0].strip()


# --- the budget ----------------------------------------------------------------------

def _int(name, default):
    try:
        return max(0, int(getattr(Config, name, default)))
    except (TypeError, ValueError):
        return default


def start_turn(conversation_id, items=1):
    """A natural prompt reached its turn: `items` stacked things (messages, a timer
    fire), each worth `[send] per_item`, `[send] max_per_turn` at most."""
    if not conversation_id:
        return
    budget = min(_int('SEND_MAX_PER_TURN', 15), _int('SEND_PER_ITEM', 3) * max(1, int(items or 1)))
    with _lock:
        _left[conversation_id] = budget


def left(conversation_id):
    with _lock:
        return _left.get(conversation_id, 0)


def _use(conversation_id):
    with _lock:
        _left[conversation_id] = max(0, _left.get(conversation_id, 0) - 1)
        return _left[conversation_id]


def _left_line(conversation_id):
    n = left(conversation_id)
    return f"{n} send{'' if n == 1 else 's'} left in this turn."


# --- sending -------------------------------------------------------------------------

def registered_group(group_id):
    """True when some profile lists this group id in its members."""
    key = str(group_id or '').strip().lower()
    if not key:
        return False
    for raw in (Config.PROFILES or {}).values():
        if any(str(m or '').strip().lower() == key for m in (raw.get('members') or [])):
            return True
    return False


def _daemon_for(profile, turn_conversation):
    """(inbox name or None, daemon url) this profile's messages leave through."""
    name = getattr(profile, 'inbox', '') or inboxes.of_conversation(turn_conversation) or None
    url = inboxes.url_of(name) if name else ''
    return name, (url or str(Config.XMTP_DAEMON_URL).rstrip('/'))


def _send_one(block, profile, turn_conversation, call):
    """Send one parsed block. Returns (conversation id, None) or (None, reason)."""
    inbox_name, url = _daemon_for(profile, turn_conversation)
    if block['kind'] == 'inbox':
        target = block['target'].lower().removeprefix('0x')
        if not _INBOX_RE.match(target):
            return None, "that is not an inbox id (64 hex characters)"
        data, why = call(url, '/dm', {'inboxId': target})
        if why:
            return None, why
        conversation = (data or {}).get('conversationId')
        if not conversation:
            return None, "the daemon opened no conversation"
        if inbox_name:
            inboxes.note(conversation, inbox_name)        # replies there leave through this inbox
        xmtp_conversation = conversation
    else:
        group = inboxes.base_conversation(block['target'])
        if not registered_group(group):
            return None, "that group is not registered (its id is in no profile's members)"
        conversation = inboxes.group_key(group, inbox_name) if inbox_name else group
        xmtp_conversation = group
    data, why = call(url, '/send', {'conversationId': xmtp_conversation, 'content': block['message']})
    if why:
        return None, why
    return conversation, None


def run(text, profile, turn_conversation, call=None, on_sent=None):
    """Act on every block in `text` and return the result text for the model.

    `call(url, path, body) -> (json, None) | (None, reason)` talks to the daemon
    (default: server.xmtp_http_bridge.daemon_call). `on_sent(conversation_id,
    block, profile)` is called for each message that left, so the caller can
    store it; what it returns is added to the result line."""
    if call is None:
        from server.xmtp_http_bridge import daemon_call as call
    blocks = parse(text)
    lines = []
    allowed = bool(getattr(profile, 'send', False))
    if allowed and len(blocks) > 1:
        return ("[SEND_XMTP_MESSAGE result]\n"
                f"Nothing sent: {len(blocks)} blocks in one answer. Send one [SEND_XMTP_MESSAGE] per answer "
                f"and wait for its result. {_left_line(turn_conversation)}")
    for block in blocks:
        if not allowed:
            lines.append("Not sent: this profile may not send messages.")
            break
        if block.get('error'):
            lines.append(f"Not sent: {block['error']}. {_left_line(turn_conversation)}")
            continue
        what = f"{'group' if block['kind'] == 'group' else 'inbox'} {block['target']}"
        if left(turn_conversation) <= 0:
            lines.append(f"Not sent to {what}: no sends left in this turn.")
            continue
        try:
            conversation, why = _send_one(block, profile, turn_conversation, call)
        except Exception as e:                      # a bad send answers, it never ends the turn
            logging.exception("send_block: send failed")
            conversation, why = None, f"{type(e).__name__}: {e}"
        if why:
            lines.append(f"Message NOT sent to {what}: {why}. {_left_line(turn_conversation)}")
            continue
        _use(turn_conversation)
        logging.info(f"send_block: profile {getattr(profile, 'name', '?')} sent to {what} "
                     f"({left(turn_conversation)} left)")
        extra = ''
        if on_sent:
            try:
                extra = on_sent(conversation, block, profile) or ''
            except Exception:
                logging.exception("send_block: on_sent failed (the message did leave)")
        lines.append(f"Message sent to {what}. {(extra + ' ') if extra else ''}{_left_line(turn_conversation)}")
    return "[SEND_XMTP_MESSAGE result]\n" + "\n".join(lines)


def result_message(conversation_id, text, kind=None):
    """The queue message that gives the turn back to the model."""
    return {
        "role": "tool",
        "source": SOURCE,
        "conversationId": conversation_id,
        "kind": kind,
        "timestamp": int(time.time() * 1000),
        "content": {"type": "text", "value": text},
    }
