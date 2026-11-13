"""Plain-HTTP link to xmtp_daemon (https://github.com/0xjmsl/xmtp_daemon).

Daemon -> gpette (LOCALHOST ONLY: these routes inject XMTP traffic):
  POST /api/xmtp/incoming       one message envelope (set the daemon's
                                XMTP_WEBHOOK_URL to http://127.0.0.1:<port>)
  POST /api/xmtp/group_members  a group's member list

gpette -> daemon:
  send_via_daemon(message) POSTs the outgoing message dict to
  {daemon.url}/send.
"""
import logging
import time

import requests
from flask import jsonify, request

from config import Config

_LOCAL = {"127.0.0.1", "::1", "localhost"}


def _local_only():
    """None when the caller is this machine, else a 403 response."""
    if request.remote_addr in _LOCAL:
        return None
    logging.warning(f"xmtp_http_bridge: refused non-local caller {request.remote_addr}")
    return jsonify({"error": "local only"}), 403


def _dropped(what, data):
    """A handler raised (call this from the `except`). Answer 422, never a 5xx:
    the daemon delivers in order and retries a 5xx with no limit, so one item
    this bot cannot process would hold every message behind it. A 4xx tells
    the daemon to drop it and move on. The traceback goes to the log."""
    logging.exception(f"xmtp_http_bridge: {what} handler failed, DROPPED "
                      f"(conversation {data.get('conversationId')}, id {str(data.get('id'))[:12]})")
    return jsonify({"error": "handler failed, dropped"}), 422


def register_routes(server, on_incoming, on_group_members):
    """Attach the two daemon -> gpette routes. The handlers are the existing
    socket.io handlers (handle_incoming_message / handle_group_members), so
    both transports run exactly the same code."""

    @server.route("/api/xmtp/incoming", methods=["POST"])
    def xmtp_incoming():
        refused = _local_only()
        if refused:
            return refused
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data.get("conversationId"):
            return jsonify({"error": "bad envelope"}), 400
        try:
            on_incoming(data)
        except Exception:
            return _dropped("message", data)
        return jsonify({"ok": True})

    @server.route("/api/xmtp/group_members", methods=["POST"])
    def xmtp_group_members():
        refused = _local_only()
        if refused:
            return refused
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "bad body"}), 400
        try:
            on_group_members(data)
        except Exception:
            return _dropped("group_members", data)
        return jsonify({"ok": True})

    logging.info("xmtp_http_bridge: routes registered (/api/xmtp/incoming, /api/xmtp/group_members)")


def send_via_daemon(message, attempts=3):
    """POST one outgoing message to the daemon. A few quick retries cover a
    daemon restart; a 4xx means the message itself is bad, so no retry.
    Returns the XMTP message id, or None."""
    url = f"{Config.XMTP_DAEMON_URL}/send"
    delay = 1
    for i in range(attempts):
        try:
            r = requests.post(url, json=message, timeout=60)
            if r.ok:
                msg_id = r.json().get("id")
                logging.info(f"xmtp_http_bridge: sent to {message.get('conversationId')} "
                             f"({message.get('contentType') or 'text'}, id {str(msg_id)[:12]})")
                return msg_id
            if 400 <= r.status_code < 500:
                logging.error(f"xmtp_http_bridge: daemon refused message ({r.status_code}): {r.text[:200]}")
                return None
            logging.warning(f"xmtp_http_bridge: daemon send failed ({r.status_code}), attempt {i + 1}/{attempts}")
        except requests.RequestException as e:
            logging.warning(f"xmtp_http_bridge: daemon unreachable ({e}), attempt {i + 1}/{attempts}")
        time.sleep(delay)
        delay *= 2
    logging.error(f"xmtp_http_bridge: message to {message.get('conversationId')} NOT sent after {attempts} attempts")
    return None
