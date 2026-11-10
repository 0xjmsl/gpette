"""Access gates: who may talk to the bot.

A gate decides, for each incoming message, one of four actions (server.py acts
on them):

    {'action': 'allow'}                          go on to the LLM
    {'action': 'reject'}                         drop silently
    {'action': 'auth_success', 'response': msg}  send `msg` back, stop there
    {'action': 'auth_limit',   'response': msg}  send `msg` back, stop there

Pick one with `[gate] kind` in gpette.toml (or `gate = ...` in a profile):

    open       everyone is answered (the default)
    none       no one is answered (drop silently); for a profile another gpette
               sharing the same inbox serves (see none.py)
    password   a stranger's first DM must be exactly `[gate] password`; groups
               are answered when any member is already in (see password.py)
    a.b.c      any other value is a dotted module path; that module must define
               a class `Gate` with `check_message(message, kind=None) -> dict`
               and, optionally, `invalidate_conversation(conversation_id)`.
               Its constructor may take one argument: the ConversationDB.

Whatever the gate, `[gate] messages_per_person_per_day` (> 0) caps how many
messages one sender inbox gets answered per day; it is applied here, after the
gate says 'allow', and is shared by every gate (a person has one budget).
"""
import importlib
import inspect
import logging
import os
import threading

from config import Config
from modules.rate_limiter import DailyRateLimiter

_gates = {}
_lock = threading.Lock()
_limiter = None


def _daily_limiter():
    global _limiter
    if _limiter is None:
        _limiter = DailyRateLimiter(0, os.path.join(Config.DATA_DIR, 'rate_limit.json'))
    _limiter.per_day = int(Config.GATE_MESSAGES_PER_PERSON_PER_DAY or 0)   # live: follows runtime edits
    return _limiter


class _GateRunner:
    """Wraps a gate: applies the per-person daily cap and normalizes errors."""

    def __init__(self, name, gate):
        self.name = name
        self.gate = gate
        params = inspect.signature(gate.check_message).parameters
        self._takes_kind = 'kind' in params or any(p.kind == p.VAR_KEYWORD for p in params.values())

    def check_message(self, message, kind=None):
        kind = kind or message.get('kind')
        try:
            if self._takes_kind:
                result = self.gate.check_message(message, kind=kind)
            else:
                result = self.gate.check_message(message)    # plug-ins without the kind argument
        except Exception as e:
            logging.error(f"Gate '{self.name}': check failed ({e}), rejecting")
            return {'action': 'reject'}
        if not isinstance(result, dict) or result.get('action') not in (
                'allow', 'reject', 'auth_success', 'auth_limit'):
            logging.error(f"Gate '{self.name}': bad decision {result!r}, rejecting")
            return {'action': 'reject'}
        if result['action'] == 'allow':
            sender = message.get('senderInboxId')
            limiter = _daily_limiter()
            if limiter.per_day > 0 and not limiter.allow(sender):
                logging.warning(f"Gate '{self.name}': daily cap reached for {sender}, dropping")
                return {'action': 'reject'}
        return result

    def invalidate_conversation(self, conversation_id):
        hook = getattr(self.gate, 'invalidate_conversation', None)
        if callable(hook):
            hook(conversation_id)


def _build(name, conversation_db):
    if name == 'open':
        from .open import OpenGate
        return OpenGate()
    if name == 'password':
        from .password import PasswordGate
        return PasswordGate(conversation_db)
    if name == 'none':
        from .none import NoneGate
        return NoneGate()
    module = importlib.import_module(name)
    cls = getattr(module, 'Gate', None)
    if cls is None:
        raise ValueError(f"gate plug-in '{name}' has no class Gate")
    params = [p for p in inspect.signature(cls).parameters.values()
              if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return cls(conversation_db) if params else cls()


def get_gate(name=None, conversation_db=None):
    """The gate called `name` (default `Config.GATE_KIND`), built once and reused."""
    name = (name or Config.GATE_KIND or 'open').strip()
    with _lock:
        runner = _gates.get(name)
        if runner is None:
            if conversation_db is None:
                from models.conversation_db import ConversationDB
                conversation_db = ConversationDB()
            runner = _GateRunner(name, _build(name, conversation_db))
            _gates[name] = runner
            logging.info(f"Gate '{name}' ready")
        return runner


def invalidate_conversation(conversation_id):
    """Tell every gate built so far that a group's member list changed."""
    with _lock:
        runners = list(_gates.values())
    for runner in runners:
        runner.invalidate_conversation(conversation_id)
