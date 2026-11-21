"""`[ADMIN_TURN]`: a profile asks its main for a turn.

A public profile talks to people and holds no power. When something needs the
profile that does (its `admin_profile`, a profile with `admin = true`), the model
ends its answer with the block:

    Alright, I'll look into that one.
    [ADMIN_TURN]

The text before the block goes to the person as the answer. The block itself
carries nothing: whatever follows it is dropped. What the main reads is the
asking profile's folder, where the model keeps its notes. gpette then puts ONE
fixed sentence in the main's queue, in the main's own chat (the newest DM one of
its `members` wrote in):

    Admin turn asked by the public profile of inbox <id>.
    Read <that person's folder> and decide.

Nothing the person or the asking model wrote is in it. It waits in the queue
like any message and is stacked with whatever else waits for that chat (the
owner's messages, other requests) into one turn. Each stacked item is worth
`[send] per_item` sends. The main's final answer goes to its own chat;
`[NO_REPLY]` sends nothing.

The token counts only when it ends a line, so a sentence that mentions it is
not a request.

Before anything is sent, four things are checked. A failed check delivers
nothing to the person and hands the turn back to the asking model with the
reason (a Claude Code run resumes with it; on another engine the block is cut
out and the rest of the answer is delivered):

- the link: `admin_profile` names a profile that exists and has `admin = true`,
  and `admin_inbox` names the one inbox (one of its members) it is woken FOR. The
  main is woken in its DM with that inbox, opened through the daemon when needed;
  nothing is looked up from history. If not: `[ADMIN_MISCONFIGURED_ERROR]`.
- the notes are the model's own words. A run of `[admin_turn] copy_words` words
  (default 6) that a person wrote in this chat, found in a file of the folder,
  refuses the request until the note is rewritten: what a stranger typed must
  never reach the main word for word. (`conversations/`, the transcripts gpette
  writes itself, is not read.)
- the caps: `[admin_turn] per_inbox_per_day` requests from one person (default
  15) and `per_day` to one main (default 50). 0 = no limit.
- a request from someone whose earlier one still waits in the queue joins it:
  the main is not woken twice, and it does not count.
"""
import logging
import os
import re
import threading
import time

from config import Config

TOKEN = "[ADMIN_TURN]"
SOURCE = "AdminTurn"                    # the wake-up message in the main's queue
RESULT_SOURCE = "AdminTurnResult"       # a refusal handed back to the asking model
ERROR = "[ADMIN_MISCONFIGURED_ERROR]"

_TOKEN_RE = re.compile(r"""[`*"']*\[\s*ADMIN[_ ]TURN\s*\][`*"' .]*$""", re.IGNORECASE | re.MULTILINE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SKIP_DIRS = ("conversations",)         # modules/transcripts.py FOLDER: gpette's own copy of the chat
_MAX_FILE = 1_000_000
_MAX_FILES = 500
_MAX_MESSAGES = 300

_lock = threading.Lock()
_pending = set()                        # (the main's conversation, who asked): waiting in the queue
daemon_call = None                      # (url, path, body) -> (json, None) | (None, reason); None = the HTTP bridge's (tests set a fake)


# --- reading the block ---------------------------------------------------------------

def has_block(text):
    return isinstance(text, str) and bool(_TOKEN_RE.search(text))


def before(text):
    """The answer for the person: what stands before the block."""
    m = _TOKEN_RE.search(text) if isinstance(text, str) else None
    return text[:m.start()].strip() if m else text


# --- the never-copy check ------------------------------------------------------------

def _words(text):
    return _WORD_RE.findall(str(text or "").lower())


def _note_files(folder):
    count = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > _MAX_FILE:
                    continue
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue                 # a picture, not a note
            count += 1
            if count > _MAX_FILES:
                return
            yield os.path.relpath(path, folder), raw.decode("utf-8", errors="ignore")


def copied(folder, messages, run=None):
    """(file, the words) for the first run of `run` words in a row that one of
    `messages` (what people wrote) shares with a file in `folder`; None when the
    notes are the model's own words."""
    n = int(getattr(Config, "ADMIN_TURN_COPY_WORDS", 6) if run is None else run)
    if n <= 0 or not folder or not os.path.isdir(folder):
        return None
    runs = set()
    for text in messages:
        w = _words(text)
        for i in range(len(w) - n + 1):
            runs.add(tuple(w[i:i + n]))
    if not runs:
        return None
    for name, text in _note_files(folder):
        w = _words(text)
        for i in range(len(w) - n + 1):
            if tuple(w[i:i + n]) in runs:
                return name.replace(os.sep, "/"), " ".join(w[i:i + n])
    return None


def _person_messages(db, conversation_id):
    try:
        rows = db.list_messages(conversation_id)
    except Exception as e:
        logging.error(f"admin_turn: could not read the chat {conversation_id}: {e}")
        return []
    out = []
    for m in rows:
        if m.get("role") != "user" or not m.get("senderInboxId"):
            continue
        c = m.get("content")
        value = c.get("value") if isinstance(c, dict) else c
        if isinstance(value, str) and value.strip():
            out.append(value)
    return out[-_MAX_MESSAGES:]


# --- the caps ------------------------------------------------------------------------

def _num(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def caps(asking_raw, target_raw, person):
    """(per person per day, per main per day); 0 = no limit."""
    per_inbox = _num(getattr(Config, "ADMIN_TURN_PER_INBOX_PER_DAY", 15), 15)
    per_day = _num(getattr(Config, "ADMIN_TURN_PER_DAY", 50), 50)
    if "admin_turns_per_inbox_per_day" in (asking_raw or {}):
        per_inbox = _num(asking_raw["admin_turns_per_inbox_per_day"], per_inbox)
    named = getattr(Config, "ADMIN_TURN_INBOXES", None) or {}
    if person in named:
        per_inbox = _num(named[person], per_inbox)
    if "admin_turns_per_day" in (target_raw or {}):
        per_day = _num(target_raw["admin_turns_per_day"], per_day)
    return per_inbox, per_day


# --- asking --------------------------------------------------------------------------

def _refusal(text):
    return {"wake": None, "error": text}


def _misconfigured(asker, why):
    logging.error(f"admin_turn: [ADMIN_TURN] of profile '{asker}' not delivered: {why}")
    return _refusal(
        f"{ERROR}\n"
        "Your [ADMIN_TURN] could not be delivered: this profile's admin link is not set up. "
        "Nothing was sent to the person. Note the request in your folder, in your own words, so it "
        "is found later. Then answer the person plainly, without the block and without mentioning "
        "an admin: you have noted their request, cannot resolve it right now, and will get back to "
        "them as soon as you can.")


def ask(profile, conversation_id, db):
    """`profile` ended an answer in `conversation_id` with [ADMIN_TURN].

    Returns {"wake": message or None, "error": text or None}. `error` = the
    request was refused: the text is for the asking model and nothing may be
    delivered to the person. Otherwise the answer's text is delivered, and
    `wake` (when not None) goes into the thread controller's queue."""
    from modules import profiles
    asker = getattr(profile, "name", "?")
    target, admin_inbox, why = profiles.admin_link(profile)
    if why:
        return _misconfigured(asker, why)
    main_conversation, why = profiles.main_conversation(target, admin_inbox, call=daemon_call)
    if why:
        return _misconfigured(asker, why)
    person = getattr(profile, "folder_key", "") or profiles.last_sender(conversation_id)
    folder = profiles.workdir_for(profile, conversation_id)

    hit = copied(folder, _person_messages(db, conversation_id))
    if hit:
        logging.warning(f"admin_turn: profile '{asker}', {str(person)[:12]}...: notes repeat the person's words "
                        f"({hit[0]}); not delivered")
        return _refusal(
            "[ADMIN_TURN result]\n"
            f"Not delivered: {hit[0]} in your folder repeats words the person wrote (\"{hit[1]} ...\"). "
            "Notes are always your own words, never a copy of a message. Nothing was sent to the person. "
            "Rewrite that note, then answer again with [ADMIN_TURN].")

    key = person or conversation_id
    with _lock:
        if (main_conversation, key) in _pending:
            logging.info(f"admin_turn: {str(key)[:12]}... already waits for '{target}'; joined")
            return {"wake": None, "error": None}
    raw = Config.PROFILES or {}
    per_inbox, per_day = caps(raw.get(asker), raw.get(target), key)
    try:
        day = db.get_today_key()
        from_person = db.count_admin_turns(target, day, key) if per_inbox else 0
        in_all = db.count_admin_turns(target, day) if per_day else 0
    except Exception as e:
        return _misconfigured(asker, f"the admin-turn count could not be read ({e})")
    if (per_inbox and from_person >= per_inbox) or (per_day and in_all >= per_day):
        logging.warning(f"admin_turn: daily limit reached for '{target}' "
                        f"({from_person}/{per_inbox} from {str(key)[:12]}..., {in_all}/{per_day} in all)")
        return _refusal(
            "[ADMIN_TURN result]\n"
            "Not delivered: today's limit of requests is reached. Nothing was sent to the person. "
            "Your notes stay in your folder and will be read later. Answer the person plainly, without "
            "the block and without mentioning an admin: you have noted their request and will get back "
            "to them as soon as you can.")
    db.add_admin_turn(target, asker, key, conversation_id)
    with _lock:
        _pending.add((main_conversation, key))
    who = f"inbox {person}" if person else f"conversation {conversation_id}"
    logging.info(f"admin_turn: profile '{asker}' ({who[:24]}...) asks '{target}' for a turn")
    return {"error": None, "wake": {
        "role": "user",
        "source": SOURCE,
        "conversationId": main_conversation,
        "kind": "dm",
        "contentType": "text",
        "timestamp": int(time.time() * 1000),
        "adminProfile": target,
        "adminKey": key,
        "content": {"type": "text", "value":
                    f"Admin turn asked by the public profile of {who}.\nRead {folder} and decide."},
    }}


def is_wake(message):
    return isinstance(message, dict) and message.get("source") == SOURCE


def done(message):
    """A wake-up left the queue (its turn began, or it was dropped): the same
    person may wake the main again."""
    if is_wake(message):
        with _lock:
            _pending.discard((message.get("conversationId"), message.get("adminKey")))


def result_message(conversation_id, text, kind=None):
    """The queue message that hands a refused request back to the asking model."""
    return {
        "role": "tool",
        "source": RESULT_SOURCE,
        "conversationId": conversation_id,
        "kind": kind,
        "timestamp": int(time.time() * 1000),
        "content": {"type": "text", "value": text},
    }
