"""Sender display names.

By default gpette does not look names up: a sender is shown by a short inbox id.
To resolve names (an address book, ENS, your own directory), set in gpette.toml:

    [senders]
    resolver = "my_package.my_resolver"

That module must define `resolve(inbox_id, address=None) -> str | None`.
Results are cached in the database for a day.
"""
import importlib
import logging

from config import Config

_resolver = None
_loaded = False


def _load():
    global _resolver, _loaded
    if _loaded:
        return _resolver
    _loaded = True
    path = (Config.SENDER_RESOLVER or '').strip()
    if path:
        try:
            _resolver = getattr(importlib.import_module(path), 'resolve')
        except Exception as e:
            logging.error(f"sender resolver '{path}' failed to load: {e}")
    return _resolver


def short_id(inbox_id: str) -> str:
    s = (inbox_id or '').lower()
    if s.startswith('0x'):
        s = s[2:]
    return f"user-{s[:6]}" if s else 'user'


def resolve(inbox_id, address=None):
    """Return (name, source). source is 'plugin' or 'fallback'."""
    fn = _load()
    if fn is not None:
        try:
            name = fn(inbox_id, address=address)
            if name:
                return str(name), 'plugin'
        except Exception as e:
            logging.warning(f"sender resolver failed for {short_id(inbox_id)}: {e}")
    return short_id(inbox_id), 'fallback'
