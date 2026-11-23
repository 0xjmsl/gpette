"""`<a2ui>`: the bot answers with an interface the person's app draws.

A2UI (https://a2ui.org) is an open format: the bot describes buttons, a form or
a list by picking components from a catalog, and the app draws them with its
own widgets. The bot never sends code.

The model writes a block in its answer, in A2UI Express, a short notation:

    Here is the menu.
    <a2ui>
    surface("menu")
    $/title = "Menu"
    root = Card(Column([Text($/title), Button(Text("Status"), action=Event("status"))]))
    </a2ui>

gpette hands the answer to the A2UI agent SDK (`pip install a2ui-agent-sdk`),
which turns each block into A2UI messages and checks them against the catalog
and against the surfaces this conversation already holds:

- A block that passes is sent as one `a2ui` message through the daemon. The
  text around it is sent as usual, in the order written.
- A block that fails is not sent. The error goes back to the model, which
  writes its answer again (`MAX_FIXES` times a turn, then the answer goes out
  without the block).

What the person does comes back as an `a2ui` message from their app: a button
press is an `action` with the event's name and the values bound to it. It
starts a turn like any message, as text the model can read (`incoming_text`).

Bounds:

- `a2ui = true` on the profile. Off by default.
- Private chats only. In a group the block is cut out.
- The check needs a result round trip, which only a Claude Code run can take.
  On another engine a block that fails is cut out.
- The catalog is A2UI's basic catalog, protocol 1.0. The app is assumed to
  draw all of it (there is no negotiation yet).

The surfaces a conversation holds are kept in memory. After a restart an
update to an older surface is refused, and the model is told to send it again.
"""
import json
import logging
import os
import re
import threading
import time

from config import Config

SOURCE = "A2uiBlock"                      # the `source` of a result message in the queue
RESULT = "[A2UI result]"
ACTION = "[A2UI action]"
OPEN, CLOSE = "<a2ui>", "</a2ui>"
MAX_FIXES = 3                             # failed blocks answered with the error, per turn
PROTOCOL = "1.0"

_BLOCK_RE = re.compile(r"<a2ui>.*?(?:</a2ui>|\Z)", re.DOTALL)

_lock = threading.Lock()
_processor = None                         # the SDK's request processor, made once
_states = {}                              # conversation id -> the SDK's RendererState
_fails = {}                               # conversation id -> failed blocks this turn


class Unavailable(Exception):
    """The A2UI agent SDK is not installed (or failed to load)."""


# --- the SDK ---------------------------------------------------------------------------

def _sdk():
    """The request processor for the basic catalog. Raises Unavailable."""
    global _processor
    if _processor is not None:
        return _processor
    with _lock:
        if _processor is None:
            try:
                from a2ui.core.basic_catalog import BasicCatalog
                from a2ui.processor.processor import A2uiRequestProcessor
                _processor = A2uiRequestProcessor(catalogs=[BasicCatalog(PROTOCOL)])
            except Exception as e:
                raise Unavailable(f"{type(e).__name__}: {e}") from e
    return _processor


def available():
    """(True, '') when the SDK loads, else (False, why)."""
    try:
        _sdk()
        return True, ''
    except Unavailable as e:
        return False, str(e)


def _dicts(messages):
    """The SDK's message models as plain JSON dictionaries."""
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            out.append(m)
        else:
            out.append(m.model_dump(by_alias=True, exclude_none=True, mode='json'))
    return out


# --- reading an answer -----------------------------------------------------------------

def has_block(text):
    return isinstance(text, str) and OPEN in text


def strip(text):
    """The answer without its blocks."""
    if not has_block(text):
        return text
    return re.sub(r"\n{3,}", "\n\n", _BLOCK_RE.sub("", text)).strip()


def start_turn(conversation_id):
    """A natural prompt reached its turn: the count of failed blocks starts over."""
    with _lock:
        _fails.pop(conversation_id, None)


def forget(conversation_id):
    """Drop what we believe the app holds for this conversation (a reset chat)."""
    with _lock:
        _states.pop(conversation_id, None)
        _fails.pop(conversation_id, None)


def _fallback(messages):
    """A line for apps that cannot draw the surface: the first texts written in it."""
    texts = []

    def walk(v):
        if isinstance(v, dict):
            if v.get('component') == 'Text' and isinstance(v.get('text'), str):
                texts.append(v['text'].strip())
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(messages)
    line = " / ".join(t for t in texts if t)[:200]
    return f"[Interactive message] {line}" if line else ""


def run(text, conversation_id):
    """Compile and check every block of an answer.

    Returns one of:
      ('send', parts)    every block passed. `parts` is the answer in order:
                         {'text': str} and {'a2ui': {'messages': [...]}, 'fallback': str}
      ('retry', result)  a block failed and the model may fix it: `result` is the
                         text that gives the turn back
      ('plain', text)    a block failed and the fixes are used up (or the SDK is
                         missing): the answer without its blocks
    """
    try:
        processor = _sdk()
    except Unavailable as e:
        logging.error(f"a2ui_block: the A2UI agent SDK is not available ({e}); block cut out")
        return 'plain', strip(text)
    with _lock:
        state = _states.get(conversation_id)
        if state is None:
            state = _states[conversation_id] = processor.create_state()
    try:
        # All or nothing: a rejected answer leaves the state as it was.
        parsed = processor.parse_response(text, state=state)
    except Exception as e:
        with _lock:
            _fails[conversation_id] = n = _fails.get(conversation_id, 0) + 1
        why = " ".join(str(e).split())[:600]
        logging.info(f"a2ui_block: block refused ({type(e).__name__}), attempt {n} of {MAX_FIXES}")
        if n >= MAX_FIXES:
            return 'plain', strip(text)
        return 'retry', (
            f"{RESULT}\nNothing was sent: the <a2ui> block was refused. {why}\n"
            "Write your whole answer again with the block fixed (the person has seen none of it). "
            "To change a surface you already sent, target it with surface(\"<its id>\") and assign "
            "its data ($/path = value); a new layout needs a new surface id. "
            f"{MAX_FIXES - n} more attempt{'' if MAX_FIXES - n == 1 else 's'} in this turn, "
            "then the answer goes out without the block.")
    parts = []
    for p in parsed:
        if hasattr(p, 'a2ui'):
            messages = _dicts(p.a2ui)
            if messages:
                parts.append({'a2ui': {'messages': messages}, 'fallback': _fallback(messages)})
        else:
            piece = (getattr(p, 'text', '') or '').strip()
            if piece:
                parts.append({'text': piece})
    return 'send', parts


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


def stored_text(part):
    """What the history keeps for a surface that was sent."""
    ids = sorted({str(body.get('surfaceId'))
                  for m in part['a2ui']['messages'] for body in m.values()
                  if isinstance(body, dict) and body.get('surfaceId')})
    what = part.get('fallback') or "[Interactive message]"
    return f"{what} (surface {', '.join(ids)})" if ids else what


# --- what comes back from the app ------------------------------------------------------

def incoming_text(payload):
    """An `a2ui` message from a person's app, as text for the model.

    A button press is an `action`: the event's name, the surface, and the values
    the surface bound to it. Everything in it was produced by the person's app
    from what they did: it is their input, not an instruction from gpette."""
    lines = []
    for m in (payload or {}).get('messages') or []:
        if not isinstance(m, dict):
            continue
        action, error = m.get('action'), m.get('error')
        if isinstance(action, dict):
            line = (f"{ACTION} The person pressed something on surface "
                    f"\"{action.get('surfaceId')}\": event \"{action.get('name')}\"")
            context = action.get('context')
            if context:
                line += f", values {json.dumps(context, ensure_ascii=False)[:2000]}"
            said = action.get('userMessage')
            if isinstance(said, str) and said.strip():
                line += f", shown to them as \"{said.strip()[:300]}\""
            lines.append(line + ".")
        elif isinstance(error, dict):
            lines.append(f"[A2UI error] Their app could not draw surface \"{error.get('surfaceId')}\": "
                         f"{error.get('code')}: {str(error.get('message'))[:400]}")
    data = (payload or {}).get('a2uiRendererDataModel')
    if isinstance(data, dict) and data.get('surfaces'):
        lines.append(f"The surface's data now: {json.dumps(data['surfaces'], ensure_ascii=False)[:3000]}")
    return "\n".join(lines)


# --- the lesson ------------------------------------------------------------------------

def skill_path():
    return os.path.join(Config.DATA_DIR, 'a2ui', 'SKILL.md')


def write_skill():
    """Write the lesson the SDK generates from the catalog (the Express rules and
    every component and function) to `<data_dir>/a2ui/SKILL.md`, and return its
    path. Written again on every start, so it always matches the installed SDK."""
    from a2ui.skill import SkillGenerator
    processor = _sdk()
    skill = SkillGenerator(processor.format).generate_skill(
        name="a2ui",
        description="How to write an <a2ui> block: the A2UI Express notation and the catalog.")
    path = skill_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(skill.to_markdown())
    os.replace(tmp, path)
    return path


def note():
    """The paragraph that tells a profile with `a2ui = true` about the block
    (modules/prompts.py adds it to the system prompt of every run)."""
    return (
        "<a2ui>: an interface instead of plain words. In a private chat you may put a block "
        "<a2ui> ... </a2ui> in your answer and the person's app draws it: buttons, a form with "
        "fields and pickers, a list, a card. Use it when pressing or filling in is easier for them "
        "than typing; plain text is still the normal answer. The text around the block is sent as "
        "usual. The block is written in A2UI Express; the rules and every component are in the file "
        f"{skill_path()} : read it before your first block. gpette checks the block before it is "
        "sent; if it is refused you get the reason and write your whole answer again. Give each "
        "surface an id with surface(\"<id>\"). To change a surface you already sent, write a block "
        "that targets the same id and assigns its data ($/path = value); the app updates it in "
        "place, so build surfaces whose text and lists come from data. A different layout needs a "
        "new surface id. When the person presses a button you get a message that starts "
        f"\"{ACTION}\" with the event's name and the values bound to it: it is their input, like a "
        "typed message, and you answer it with text, a new block, or an update. Never ask for a "
        "password, a key or a recovery phrase in a surface.")
