"""Short tags for XMTP messages, so a model can point at one.

Every message the bot sees or sends gets a tag, `m:` + the first 6 hex of its
XMTP id (`m:3fa9c1`). Incoming text reaches the model with its tag in front:

    [m:3fa9c1] how's it going?
    [m:7b20aa, reply to m:3fa9c1 "how's it going?"] fine, you?
    [m:9c01de] [reaction 🫡 added to m:3fa9c1 "how's it going?"]

The model uses a tag to react or reply ([SEND_XMTP_MESSAGE] with TO:, or the
turn-ending [REPLY] block). `resolve` turns it back into the full id, inside
one conversation only: a tag from another chat is refused, never guessed.

The map lives in memory, the last `KEEP` messages per conversation; after a
restart older tags no longer resolve (the model is told so). A full 64-hex id
is accepted as is.
"""
import re
import threading
from collections import OrderedDict

KEEP = 500
PREVIEW = 60
_TAG_RE = re.compile(r"^\s*m:([0-9a-f]{4,64})\s*$", re.IGNORECASE)
_FULL_RE = re.compile(r"^[0-9a-f]{64}$")

_lock = threading.Lock()
_by_conv = {}            # conversation key -> OrderedDict(full id -> preview)


def _key(conversation_id):
    try:
        from modules import inboxes
        return inboxes.base_conversation(conversation_id) or conversation_id
    except Exception:
        return conversation_id


def tag(message_id):
    return f"m:{str(message_id or '')[:6].lower()}" if message_id else ""


def _preview(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= PREVIEW else text[:PREVIEW - 1] + "…"


def remember(conversation_id, message_id, text=""):
    """Note a message (incoming or sent) so its tag resolves later."""
    if not conversation_id or not message_id:
        return
    with _lock:
        msgs = _by_conv.setdefault(_key(conversation_id), OrderedDict())
        msgs[str(message_id).lower()] = _preview(text)
        msgs.move_to_end(str(message_id).lower())
        while len(msgs) > KEEP:
            msgs.popitem(last=False)


def resolve(conversation_id, ref):
    """(full id, None) or (None, why). `ref` is a tag or a full id."""
    ref = str(ref or "").strip()
    if _FULL_RE.match(ref.lower()):
        return ref.lower(), None
    m = _TAG_RE.match(ref)
    if not m:
        return None, f"'{ref}' is not a message tag (m: plus 6 hex, as shown before a message)"
    prefix = m.group(1).lower()
    with _lock:
        msgs = _by_conv.get(_key(conversation_id)) or {}
        hits = [i for i in msgs if i.startswith(prefix)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, f"m:{prefix} matches {len(hits)} messages; use more of the id"
    return None, f"m:{prefix} is not a message in this chat (unknown, or from before a restart)"


def describe(conversation_id, message_id):
    """`m:3fa9c1 "first words…"` for a message we know, else just the tag."""
    with _lock:
        text = (_by_conv.get(_key(conversation_id)) or {}).get(str(message_id or "").lower())
    t = tag(message_id)
    return f'{t} "{text}"' if text else t


def incoming_line(conversation_id, envelope, text):
    """The text the model sees for one incoming envelope (`id`, `replyTo`,
    `reaction` as xmtp_daemon sends them), and the message noted for later."""
    mid = envelope.get("id")
    reply_to = envelope.get("replyTo")
    reaction = envelope.get("reaction") if isinstance(envelope.get("reaction"), dict) else None
    if reaction:
        verb = "removed from" if reaction.get("action") == "removed" else "added to"
        body = f"[reaction {text} {verb} {describe(conversation_id, reaction.get('reference'))}]"
    else:
        body = text
    head = tag(mid)
    if reply_to:
        head = f"{head}, reply to {describe(conversation_id, reply_to)}" if head else \
            f"reply to {describe(conversation_id, reply_to)}"
    remember(conversation_id, mid, body if not reaction else f"(reaction {text})")
    return f"[{head}] {body}" if head else body
