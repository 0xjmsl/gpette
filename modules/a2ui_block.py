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
- The catalog is A2UI's basic catalog, protocol 1.0, unless the profile names
  another one: `a2ui_catalog = "<path to a catalog document>"` (the JSON file a
  catalog is published as). A profile writes for one catalog. The lesson and
  the check are then made from that document, and each catalog has its own
  lesson file.
- The app is assumed to draw all of the profile's catalog (there is no
  negotiation yet), unless the profile says what the app draws:
  `a2ui_draws = "<path to a JSON file>"`, holding
  `{"components": ["Card", ...], "functions": ["formatDate", ...]}`. Either
  list may be left out (= all of that kind). The lesson then teaches only
  those, and a block that uses anything else is refused.

The surfaces a conversation holds are kept in memory. After a restart an
update to an older surface is refused, and the model is told to send it again.
"""
import hashlib
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
_processors = {}                          # _key(spec) -> the SDK's request processor, made once
_states = {}                              # conversation id -> (_key(spec), the SDK's RendererState)
_fails = {}                               # conversation id -> failed blocks this turn


class Unavailable(Exception):
    """The A2UI agent SDK is not installed (or failed to load)."""


# --- the SDK ---------------------------------------------------------------------------

def spec_of(profile):
    """What a profile writes its blocks for: (its `a2ui_catalog`, its
    `a2ui_draws`). Every function below that takes a `spec` takes this; the
    empty one is the whole basic catalog."""
    return (getattr(profile, 'a2ui_catalog', '') or '', getattr(profile, 'a2ui_draws', '') or '')


def _key(spec):
    """A spec with its two paths made absolute ('' stays ''): the key of its
    processor. ('', '') is the whole basic catalog."""
    def full(path):
        path = (path or '').strip()
        return os.path.abspath(os.path.expanduser(path)) if path else ''
    spec = tuple(spec or ())
    return (full(spec[0] if len(spec) > 0 else ''), full(spec[1] if len(spec) > 1 else ''))


def _names(draws, kind):
    """The list of `kind` in an `a2ui_draws` file, or None when it has none."""
    names = draws.get(kind)
    if names is None:
        return None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(f"'{kind}' must be a list of names")
    return names


def _sdk(spec=()):
    """The request processor for a spec: the basic catalog or the document a
    profile names, cut down to what its app draws when it says so. Raises
    Unavailable (no SDK, or a file that does not load)."""
    key = _key(spec)
    document, draws_file = key
    processor = _processors.get(key)
    if processor is not None:
        return processor
    with _lock:
        processor = _processors.get(key)
        if processor is None:
            try:
                from a2ui.processor.processor import A2uiRequestProcessor
                if document:
                    from a2ui.processor.catalog_config import CatalogConfig
                    api = CatalogConfig.from_path(document).transformed_catalog
                else:
                    from a2ui.core.basic_catalog import BasicCatalog
                    api = BasicCatalog(PROTOCOL)
                if draws_file:
                    from a2ui.catalog_transformers.pruning import (
                        ComponentPruningTransformer, FunctionPruningTransformer)
                    with open(draws_file, encoding='utf-8') as f:
                        draws = json.load(f)
                    if not isinstance(draws, dict):
                        raise ValueError("a2ui_draws must hold a JSON object")
                    components, functions = _names(draws, 'components'), _names(draws, 'functions')
                    if components is not None:
                        unknown = sorted(set(components) - set(api.components))
                        if unknown:
                            raise ValueError(f"a2ui_draws names components the catalog does not have: {unknown[:10]}")
                        api = ComponentPruningTransformer(components).transform(api)
                    if functions is not None:
                        api = FunctionPruningTransformer(functions).transform(api)
                processor = _processors[key] = A2uiRequestProcessor(catalogs=[api])
            except Exception as e:
                raise Unavailable(f"{type(e).__name__}: {e}") from e
    return processor


def available(spec=()):
    """(True, '') when the SDK loads (and the files the spec names, if any),
    else (False, why)."""
    try:
        _sdk(spec)
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
    """A line for apps that cannot draw the surface: the first texts written in it
    (a component's `text`, or its `title` when it has no text)."""
    texts = []

    def walk(v):
        if isinstance(v, dict):
            if isinstance(v.get('component'), str):
                for name in ('text', 'title'):
                    if isinstance(v.get(name), str) and v[name].strip():
                        texts.append(v[name].strip())
                        break
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(messages)
    line = " / ".join(t for t in texts if t)[:200]
    return f"[Interactive message] {line}" if line else ""


def run(text, conversation_id, spec=()):
    """Compile and check every block of an answer against what the profile
    writes for (`spec_of(profile)`; the empty spec is the whole basic catalog).

    Returns one of:
      ('send', parts)    every block passed. `parts` is the answer in order:
                         {'text': str} and {'a2ui': {'messages': [...]}, 'fallback': str}
      ('retry', result)  a block failed and the model may fix it: `result` is the
                         text that gives the turn back
      ('plain', text)    a block failed and the fixes are used up (or the SDK is
                         missing): the answer without its blocks
    """
    try:
        processor = _sdk(spec)
    except Unavailable as e:
        logging.error(f"a2ui_block: the block cannot be checked ({e}); block cut out")
        return 'plain', strip(text)
    key = _key(spec)
    with _lock:
        held = _states.get(conversation_id)
        if held is None or held[0] != key:    # a state belongs to one catalog
            held = _states[conversation_id] = (key, processor.create_state())
        state = held[1]
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

def skill_path(spec=()):
    """Where the lesson of a spec is: `<data_dir>/a2ui/SKILL.md` for the whole
    basic catalog, `<data_dir>/a2ui/<name>/SKILL.md` for any other (the name is
    made from the spec's paths, so two specs never share a file)."""
    key = _key(spec)
    if key == ('', ''):
        return os.path.join(Config.DATA_DIR, 'a2ui', 'SKILL.md')
    stem = os.path.splitext(os.path.basename(key[0]))[0].lower() if key[0] else 'basic'
    stem = re.sub(r'[^a-z0-9]+', '-', stem).strip('-') or 'catalog'
    mark = hashlib.sha256(os.path.normcase('|'.join(key)).encode('utf-8')).hexdigest()[:8]
    return os.path.join(Config.DATA_DIR, 'a2ui', f"{stem}-{mark}", 'SKILL.md')


def write_skill(spec=()):
    """Write the lesson the SDK generates from the catalog (the Express rules and
    every component and function the profile may use) to `skill_path(spec)`,
    and return its path. Written again on every start, so it always matches the
    installed SDK and the files the spec names."""
    from a2ui.skill import SkillGenerator
    processor = _sdk(spec)
    skill = SkillGenerator(processor.format).generate_skill(
        name="a2ui",
        description="How to write an <a2ui> block: the A2UI Express notation and the catalog.")
    path = skill_path(spec)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(skill.to_markdown())
    os.replace(tmp, path)
    return path


def note(spec=()):
    """The paragraph that tells a profile with `a2ui = true` about the block
    (modules/prompts.py adds it to the system prompt of every run). `spec` is
    `spec_of(profile)`: it decides which lesson file is named."""
    return (
        "<a2ui>: an interface instead of plain words. In a private chat you may put a block "
        "<a2ui> ... </a2ui> in your answer and the person's app draws it: buttons, a form with "
        "fields and pickers, a list, a card, and whatever else the catalog in the lesson file "
        "holds. Use only components and properties that file lists. Use it when pressing or filling in is easier for them "
        "than typing; plain text is still the normal answer. The text around the block is sent as "
        "usual. The block is written in A2UI Express; the rules and every component are in the file "
        f"{skill_path(spec)} : read it before your first block. gpette checks the block before it is "
        "sent; if it is refused you get the reason and write your whole answer again. Give each "
        "surface an id with surface(\"<id>\"). To change a surface you already sent, write a block "
        "that targets the same id and assigns its data ($/path = value); the app updates it in "
        "place, so build surfaces whose text and lists come from data. A different layout needs a "
        "new surface id. When the person presses a button you get a message that starts "
        f"\"{ACTION}\" with the event's name and the values bound to it: it is their input, like a "
        "typed message, and you answer it with text, a new block, or an update. Never ask for a "
        "password, a key or a recovery phrase in a surface.")
