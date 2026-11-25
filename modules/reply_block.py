"""`[REPLY]`: end a turn with a reply to one message in particular.

    [REPLY]
    {TO: m:3fa9c1,
    MESSAGE: the answer}

The block is the whole answer. It goes to the chat the turn belongs to, as an
XMTP reply to the tagged message (modules/msg_tags.py). A tag that does not
resolve never loses the answer: the text is delivered as a plain message.
"""
import re

TOKEN = "[REPLY]"
_TOKEN_RE = re.compile(r"^[ \t>*`]*\[\s*REPLY\s*\][ \t*`]*$", re.IGNORECASE | re.MULTILINE)
_TO_RE = re.compile(r"""^\s*["']?TO["']?\s*:\s*["']?([^\s"',}]+)["']?\s*,?\s*""", re.IGNORECASE)
_MESSAGE_RE = re.compile(r"""^\s*["']?MESSAGE["']?\s*:\s*""", re.IGNORECASE)


def has_block(text):
    return isinstance(text, str) and bool(_TOKEN_RE.search(text))


def parse(text):
    """{'to', 'message'} or {'error', 'message'?} (message = the best text to
    deliver anyway)."""
    body = _TOKEN_RE.split(text, maxsplit=1)[1]
    start, end = body.find('{'), body.rfind('}')
    if start < 0 or end <= start:
        return {'error': 'the [REPLY] block has no {…} body', 'message': body.strip()}
    inner = body[start + 1:end]
    m = _TO_RE.match(inner)
    rest = inner[m.end():] if m else inner
    mm = _MESSAGE_RE.match(rest)
    message = rest[mm.end():] if mm else rest
    message = message.strip()
    if len(message) >= 2 and message[0] == message[-1] and message[0] in "\"'":
        message = message[1:-1].strip()
    if not m:
        return {'error': 'the [REPLY] block needs TO: <message tag>', 'message': message}
    if not message:
        return {'error': 'the [REPLY] MESSAGE is empty'}
    return {'to': m.group(1).strip(), 'message': message}
