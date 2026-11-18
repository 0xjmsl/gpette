"""The built-in sender-name lookup: PublicOS (the default `[senders] resolver = "publicos"`).

An XMTP inbox that belongs to a PublicOS account has that account's display name on
the PublicOS chain. Two read-only calls, no key, no account needed:

    inbox id --XmtpMemory.getAccountAddressByInboxId--> account --AccountMemory.getDisplayName--> name

Contract addresses are read from the ModuleRegistry (the `mr_anchor`), never pinned, so
a contract upgrade needs no change here. Defaults: the public RPC and the production
anchor; override them in `[senders]` (`rpc`, `mr_anchor`), or set `resolver = ""` to
turn name lookup off, or point `resolver` at your own module.

Returns None when the inbox has no account or the account has no name; gpette then
shows a short inbox id. Any network error also returns None (and is logged once per
failure), so a chain outage never blocks a chat.
"""
import logging
import threading

import requests

from config import Config

DEFAULT_RPC = "https://rpc.publicos.dev"
DEFAULT_ANCHOR = "0xF69a649A5541d2d0108E00f697647fF1dD4Bd4bD"  # ModuleRegistry, PublicOS production chain (4489)

# keccak256 of the ModuleRegistry type names (stable across deploys).
_ACCOUNT_MEMORY = "f4f67ed407d00bb6031ed05eefd20c18fe270cdc76c9705ddceba0d3e28430c1"
_XMTP_MEMORY = "d0adb7017a06d5c710b3e32b4a6b910ccba67976415c88943d23879434236b10"
# 4-byte selectors.
_SEL_GET_MODULE = "22dbefbb"        # getModuleAddress(bytes32)
_SEL_ACCOUNT_BY_INBOX = "2c3bd85c"  # getAccountAddressByInboxId(bytes)
_SEL_DISPLAY_NAME = "1042876d"      # getDisplayName(address)

_UA = "gpette (+https://github.com/0xjmsl/gpette)"
_modules = {}
_lock = threading.Lock()


def _word(h):
    return h.rjust(64, "0")


def _call(to, data):
    rpc = (getattr(Config, "SENDER_RPC", "") or DEFAULT_RPC).strip()
    r = requests.post(rpc, timeout=8, headers={"User-Agent": _UA}, json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": "0x" + data}, "latest"]})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"].removeprefix("0x")


def _module(type_hash):
    with _lock:
        if type_hash in _modules:
            return _modules[type_hash]
    anchor = (getattr(Config, "SENDER_MR_ANCHOR", "") or DEFAULT_ANCHOR).strip()
    out = _call(anchor, _SEL_GET_MODULE + type_hash)
    if len(out) < 40 or int(out, 16) == 0:
        raise RuntimeError("module did not resolve (wrong mr_anchor or rpc?)")
    addr = "0x" + out[-40:]
    with _lock:
        _modules[type_hash] = addr
    return addr


def resolve(inbox_id, address=None):
    norm = (inbox_id or "").lower().removeprefix("0x").strip()
    if not norm or len(norm) % 2:
        return None
    try:
        n = len(norm) // 2
        arg = _word("20") + _word(format(n, "x")) + norm + "0" * ((64 - len(norm) % 64) % 64)
        out = _call(_module(_XMTP_MEMORY), _SEL_ACCOUNT_BY_INBOX + arg)
        if not out or int(out, 16) == 0:
            return None
        account = out[-40:]
        out = _call(_module(_ACCOUNT_MEMORY), _SEL_DISPLAY_NAME + _word(account))
        if len(out) < 128:
            return None
        size = int(out[64:128], 16)
        name = bytes.fromhex(out[128:128 + 2 * size]).decode("utf-8", "replace").strip()
        return name or None
    except Exception as e:
        logging.warning(f"publicos_names: lookup failed for {norm[:12]}... ({e})")
        with _lock:
            _modules.clear()  # a redeploy self-heals on the next call
        return None
