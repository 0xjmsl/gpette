"""`/menu` for admin profiles, and how a failed model run is answered.

The menu
--------
A profile with `admin = true` gets `/menu`: a message of buttons (the
`coinbase.com/actions` content type; xmtp_daemon sends it, apps that know it
show buttons, every other app shows the numbered fallback text). A pressed
button comes back as an intent (`contentType: "intent"`), a typed number works
the same. Everything here runs BEFORE the model, so it still answers while
the model is over its limit.

    /menu            the main menu
    /status          the status screen
    /model <name>    switch this profile's model (opus | sonnet | haiku | fable |
                     any model id); `/model default` goes back to gpette.toml

Session > "Compact now" and "New session" pass `/compact` and `/reset` on to the
chat (modules/context_compactor.py owns both; they work typed too, on any
profile with a Claude Code chat).

On a profile without `admin`, none of this exists: `/menu` is ordinary text.

Errors
------
When the Claude Code run fails, the reason is sorted into a kind (limit,
overloaded, auth, timeout, session, other) and answered per profile:

    on_error = "describe"   the kind, the reason, the model and /menu
                            (the default for admin profiles)
    on_error = "generic"    the profile's `error_reply` text
                            (the default for every other profile)

A timer fire reports a failure once per streak: the first failure says so,
the next ones send nothing, and the first success after them says "back".

State (model overrides, paused timers, recent errors, costs, and what each
Claude Code session held and cost after its last run) lives in
`<data_dir>/menu_state.json`, so a model switch survives a restart.
"""
import json
import logging
import os
import threading
import time
import uuid

from config import Config

GENERIC_REPLY = "I'm not available right now. Please try again in a while."
MODELS = ["opus", "sonnet", "haiku", "fable"]

_lock = threading.Lock()
_state = None
# The last menu sent per conversation: {conversation_id: (menu_id, [action ids])},
# so a typed number maps to the same button.
_open_menus = {}


# --- state ------------------------------------------------------------------

def _path():
    return os.path.join(Config.DATA_DIR, 'menu_state.json')


def _load():
    global _state
    if _state is None:
        try:
            with open(_path(), 'r', encoding='utf-8') as f:
                _state = json.load(f)
        except (OSError, json.JSONDecodeError):
            _state = {}
        for key, empty in (('models', {}), ('errors', []), ('streaks', {}),
                           ('costs', {}), ('last_turn', {}), ('timers_paused', False)):
            _state.setdefault(key, empty)
        if 'sessions' not in _state:
            # A state file from before sessions were tracked on their own.
            _state['sessions'] = {
                t['session']: {'total': t.get('total', 0), 'context': t.get('context', 0), 't': t.get('t', 0)}
                for t in _state['last_turn'].values() if t.get('session')}
    return _state


def _save():
    tmp = _path() + '.tmp'
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_state, f, indent=1)
        os.replace(tmp, _path())
    except OSError as e:
        logging.error(f"admin_menu: cannot save state: {e}")


def model_override(profile_name):
    with _lock:
        return _load()['models'].get(profile_name)


def apply_overrides(profile):
    """Called when a profile is resolved: a model picked in the menu wins."""
    m = model_override(profile.name)
    if m:
        profile.model = m
    return profile


def timers_paused():
    with _lock:
        return bool(_load()['timers_paused'])


# --- errors -----------------------------------------------------------------

def classify(reason):
    r = (reason or '').lower()
    if any(k in r for k in ('usage limit', 'usage_credits', 'rate_limit', 'rate limit',
                            '429', 'limit reached', 'hit your limit', 'quota')):
        return 'limit'
    if any(k in r for k in ('overloaded', '529', 'capacity')):
        return 'overloaded'
    if any(k in r for k in ('401', '403', 'auth', 'login', 'credential', 'api key', 'token expired')):
        return 'auth'
    if 'timed out' in r or 'timeout' in r:
        return 'timeout'
    if 'no conversation found' in r or 'session' in r and 'not found' in r:
        return 'session'
    return 'other'


def is_stale_session(reason):
    """Only a missing session justifies dropping it and retrying fresh. A limit
    or an auth error says nothing about the session, and dropping it loses the
    whole conversation's memory."""
    return classify(reason) in ('session', 'other')


def _describe(kind, reason, profile):
    head = {
        'limit': 'Usage limit reached',
        'overloaded': 'The model is overloaded',
        'auth': 'Claude Code is not signed in (or the token expired)',
        'timeout': 'The run timed out',
        'session': 'The session could not be resumed',
        'other': 'The model run failed',
    }[kind]
    model = profile.model or 'default'
    text = f"{head} ({model}).\n{(reason or '').strip()[:500]}"
    if profile.admin:
        text += "\n\n/menu to switch the model or pause the timers."
    return text


def error_reply(profile, conversation_id, reason, is_timer):
    """What to send for a failed run. None = send nothing (a timer fire inside
    a failure streak that was already reported)."""
    kind = classify(reason)
    now = int(time.time())
    with _lock:
        st = _load()
        st['errors'] = (st['errors'] + [{
            't': now, 'profile': profile.name, 'conversation': conversation_id,
            'kind': kind, 'model': profile.model or 'default', 'reason': (reason or '')[:1000],
        }])[-20:]
        streak = st['streaks'].get(conversation_id)
        if is_timer:
            if streak:
                streak['count'] += 1
                _save()
                return None
            st['streaks'][conversation_id] = {'since': now, 'kind': kind, 'count': 1}
        _save()
    if profile.on_error != 'describe':
        return profile.error_reply or GENERIC_REPLY
    text = _describe(kind, reason, profile)
    if is_timer:
        text = (f"Timer fires are failing since {time.strftime('%H:%M', time.localtime(now))}. "
                f"I'll stay quiet until one works.\n{text}")
    return text


_INPUT_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def context_tokens(result):
    """The tokens of context a session holds when a run ends.

    Claude Code's `usage` is summed over every model call of the run, so it
    counts the context once per call. `usage.iterations` is the last call
    alone: what it read (fresh and cached) plus what it wrote is what the
    session now holds. A CLI that does not report it: tokens / calls."""
    usage = result.get('usage') or {}
    iterations = usage.get('iterations') or []
    last = iterations[-1] if iterations and isinstance(iterations[-1], dict) else None
    if last:
        return sum(int(last.get(k) or 0) for k in _INPUT_KEYS + ('output_tokens',))
    tokens = sum(int(usage.get(k) or 0) for k in _INPUT_KEYS)
    return tokens // max(1, int(result.get('num_turns') or 1))


def session_context(session_id):
    """Tokens of context a session held after its last run (0 = not known)."""
    with _lock:
        return int((_load()['sessions'].get(session_id) or {}).get('context') or 0)


def _add_cost(st, usd):
    day = time.strftime('%Y-%m-%d')
    st['costs'] = {d: c for d, c in st['costs'].items() if d >= time.strftime(
        '%Y-%m-%d', time.localtime(time.time() - 31 * 86400))}
    st['costs'][day] = round(st['costs'].get(day, 0) + usd, 4)


def note_cost(usd):
    """A run outside any chat session (a compaction call): count it in the day."""
    with _lock:
        _add_cost(_load(), float(usd or 0))
        _save()


def note_success(profile, conversation_id, result, throwaway=False):
    """A run worked: close a failure streak (returns a "back" line to send
    first, for describe profiles) and count the cost.

    throwaway: a run in a session of its own that is not kept (a timer with
    skip_session). Its cost counts; the conversation's last turn and session
    stay as they were."""
    back = None
    with _lock:
        st = _load()
        streak = st['streaks'].pop(conversation_id, None)
        if streak and profile.on_error == 'describe':
            since = time.strftime('%H:%M', time.localtime(streak['since']))
            back = f"Back. {streak['count']} timer fire(s) failed since {since} ({streak['kind']})."
        # Claude Code reports `total_cost_usd` as the SESSION's running total
        # (it is carried across --resume). So this run's cost is the difference
        # from the last total of the same session.
        total = float(result.get('total_cost_usd') or 0)
        session = result.get('session_id')
        prev = st['sessions'].get(session) if session else None
        cost = total - prev['total'] if prev and total >= prev.get('total', 0) else total
        _add_cost(st, cost)
        if not throwaway:
            now, context = int(time.time()), context_tokens(result)
            if session:
                st['sessions'][session] = {'total': round(total, 4), 'context': context, 't': now}
                if len(st['sessions']) > 50:
                    keep = sorted(st['sessions'], key=lambda k: st['sessions'][k].get('t', 0))[-50:]
                    st['sessions'] = {k: st['sessions'][k] for k in keep}
            st['last_turn'][conversation_id] = {'t': now, 'cost': round(cost, 4), 'context': context,
                                                'session': session, 'total': round(total, 4),
                                                'model': profile.model or 'default'}
        _save()
    return back


# --- the menu ---------------------------------------------------------------

def _menu(conversation_id, description, buttons):
    """buttons: [(action_id, label)]. Returns the outgoing message."""
    menu_id = uuid.uuid4().hex[:12]
    _open_menus[conversation_id] = (menu_id, [a for a, _ in buttons], time.time())
    return {
        'conversationId': conversation_id,
        'contentType': 'actions',
        'actions': {
            'id': menu_id,
            'description': description,
            'actions': [{'id': a, 'label': label,
                         'style': 'primary' if i == 0 else 'secondary'}
                        for i, (a, label) in enumerate(buttons)],
        },
    }


def _text(conversation_id, text):
    # The shape server.sendMessage takes for text: content = {type, value}.
    return {'conversationId': conversation_id, 'content': {'type': 'text', 'value': text}}


def _main(cid):
    return _menu(cid, 'Menu', [('status', 'Status'), ('model', 'Model'), ('session', 'Session'),
                               ('timers', 'Timers'), ('errors', 'Errors'), ('help', 'Help')])


def _ago(t):
    s = int(time.time()) - int(t)
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def _status(cid, profile, conversation_db):
    with _lock:
        st = _load()
        turn = st['last_turn'].get(cid)
        today = st['costs'].get(time.strftime('%Y-%m-%d'), 0)
        errors = [e for e in st['errors'] if e['profile'] == profile.name]
        paused = st['timers_paused']
        override = st['models'].get(profile.name)
    lines = [f"Profile: {profile.name}",
             f"Model: {profile.model or 'default'}" + (" (set in the menu)" if override else "")]
    if turn:
        lines.append(f"Last turn: ${turn['cost']:.2f}, {_ago(turn['t'])}")
        if turn.get('context'):
            lines.append(f"Session: about {turn['context']:,} tokens of context"
                         + (f", ${turn['total']:.2f} so far" if turn.get('total') else ""))
    if conversation_db is not None:
        try:
            from modules.context_compactor import summary_key
            saved = conversation_db.get_summary(summary_key(cid))
            if saved and saved.get('summary'):
                lines.append(f"Summary: {len(saved['summary'].split())} words, "
                             f"updated {_ago(saved.get('updated_at') or 0)}")
        except Exception as e:
            logging.warning(f"admin_menu: summary lookup failed: {e}")
    lines.append(f"Today: ${today:.2f} (counted since the menu landed)")
    if errors:
        e = errors[-1]
        lines.append(f"Last error: {e['kind']}, {_ago(e['t'])} on {e['model']}: {e['reason'][:200]}")
    else:
        lines.append("Last error: none")
    lines.append("Timers: PAUSED" if paused else "Timers: running")
    if conversation_db is not None:
        try:
            timers = conversation_db.get_timers_for_conversation(cid) or []
            for t in sorted(timers, key=lambda t: t.get('fire_at', 0))[:3]:
                at = time.strftime('%H:%M', time.localtime(t.get('fire_at', 0)))
                lines.append(f"  next {at}: {t.get('description', '')[:40]}")
        except Exception as e:
            logging.warning(f"admin_menu: timer list failed: {e}")
    return _text(cid, "\n".join(lines))


def _set_model(cid, profile, name):
    name = (name or '').strip()
    with _lock:
        st = _load()
        if name.lower() in ('', 'default'):
            st['models'].pop(profile.name, None)
            msg = "Model back to the gpette.toml default. It applies from the next message."
        else:
            st['models'][profile.name] = name
            msg = f"Model set to {name} for the {profile.name} profile. It applies from the next message."
        _save()
    return _text(cid, msg)


def _set_paused(cid, paused):
    with _lock:
        _load()['timers_paused'] = paused
        _save()
    return _text(cid, "Timers paused. They keep their schedule but don't fire."
                 if paused else "Timers running again.")


def _errors(cid, profile):
    with _lock:
        errors = [e for e in _load()['errors'] if e['profile'] == profile.name][-5:]
    if not errors:
        return _text(cid, "No errors recorded.")
    lines = [f"{time.strftime('%m-%d %H:%M', time.localtime(e['t']))} {e['kind']} on {e['model']}: "
             f"{e['reason'][:300]}" for e in reversed(errors)]
    return _text(cid, "\n\n".join(lines))


HELP = ("Shortcuts: /menu · /status · /model <opus|sonnet|haiku|fable|default> · /compact · /reset\n"
        "Buttons: hold one to choose. In other apps, answer with the number.")


def _act(action, cid, profile, conversation_db):
    """One button. Returns (messages to send, text to pass on to the model or None)."""
    if action == 'status':
        return [_status(cid, profile, conversation_db)], None
    if action == 'model':
        cur = profile.model or 'default'
        return [_menu(cid, f"Model (now {cur})",
                      [(f"model:{m}", m) for m in MODELS] + [('model:default', 'Default')])], None
    if action.startswith('model:'):
        return [_set_model(cid, profile, action.split(':', 1)[1])], None
    if action == 'session':
        return [_menu(cid, 'Session', [('session:compact', 'Compact now'),
                                       ('session:new', 'New session'), ('menu', 'Back')])], None
    # Both go on to the chat as text: modules/context_compactor.py acts on the
    # right session and summary, and the answer comes back as a normal reply.
    if action == 'session:compact':
        return [], '/compact'
    if action == 'session:new':
        return [], '/reset'
    if action == 'timers':
        paused = timers_paused()
        return [_menu(cid, 'Timers (paused)' if paused else 'Timers (running)',
                      [('timers:resume', 'Resume all') if paused else ('timers:pause', 'Pause all'),
                       ('status', 'Show next'), ('menu', 'Back')])], None
    if action == 'timers:pause':
        return [_set_paused(cid, True)], None
    if action == 'timers:resume':
        return [_set_paused(cid, False)], None
    if action == 'errors':
        return [_errors(cid, profile)], None
    if action == 'help':
        return [_text(cid, HELP)], None
    if action == 'menu':
        return [_main(cid)], None
    return [_text(cid, "That button is no longer valid. /menu for a fresh one.")], None


def _text_of(message):
    content = message.get('content')
    text = (content.get('value') if isinstance(content, dict) else content) or ''
    return text.strip() if isinstance(text, str) else ''


def menu_action(message, profile):
    """The menu action this message asks for (a button id), or None when it is
    ordinary chat. A pressed button, `/menu`, `/status`, `/model [name]`, or a
    typed number right after a menu."""
    if not getattr(profile, 'admin', False):
        return None
    ctype = message.get('contentType') or 'text'
    text = _text_of(message)
    if ctype == 'intent':
        return (message.get('intent') or {}).get('actionId') or text or 'unknown'
    if ctype != 'text':
        return None
    low = text.lower()
    if low in ('/menu', 'menu'):
        return 'menu'
    if low == '/status':
        return 'status'
    if low.startswith('/model'):
        arg = text[len('/model'):].strip()
        return f'model:{arg}' if arg else 'model'
    # A typed number answers the last menu sent here (apps without buttons).
    # Only right after a menu (10 min), so a number in normal chat stays chat.
    cid = message.get('conversationId')
    if low.isdigit() and cid in _open_menus:
        _, actions, sent = _open_menus[cid]
        n = int(low)
        if time.time() - sent < 600 and 1 <= n <= len(actions):
            return actions[n - 1]
    return None


def handle(message, profile, conversation_db=None):
    """Called for every allowed incoming message, before the model.

    Returns (messages_to_send, consumed, replacement_text):
    - consumed=False: not a menu message, carry on as normal;
    - consumed=True: send `messages_to_send` and stop, unless replacement_text
      is set, in which case the message carries on with that text."""
    action = menu_action(message, profile)
    if action is None:
        return [], False, None
    cid = message.get('conversationId')
    if (message.get('contentType') or 'text') == 'text' and _text_of(message).isdigit():
        _open_menus.pop(cid, None)   # a typed number answers a menu once
    msgs, replacement = _act(action, cid, profile, conversation_db)
    return msgs, True, replacement


def failure_notice(message, profile, error):
    """The menu raised (in `handle`, or while its answer was being sent). Returns
    the text message to send instead, or None when the message was ordinary
    chat, which then goes on to the model as if there were no menu.

    The press itself is dropped: the caller sends this and stops, so the
    conversation keeps working while the menu is broken."""
    try:
        action = menu_action(message, profile)
    except Exception:
        # Could not even be read: a press is dropped, text goes on as chat.
        if message.get('contentType') != 'intent':
            return None
        action = 'unknown'
    if action is None:
        # A typed number is a press too: `handle` closes the menu it answers
        # before acting, so by now `menu_action` no longer sees one.
        text = _text_of(message)
        if not (getattr(profile, 'admin', False) and text.isdigit()):
            return None
        action = text
    return _text(message.get('conversationId'),
                 f"The menu failed on \"{action}\" and that press was dropped.\n"
                 f"{type(error).__name__}: {str(error)[:300]}\n\n"
                 "You can keep chatting normally. The full error is in the log.")
