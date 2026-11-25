"""Message tags, reactions and replies (modules/msg_tags.py, send_block TO:/REACTION:,
modules/reply_block.py). A fake daemon answers /dm and /send. No Claude, no XMTP.

    python tests/test_msg_tags.py

Exit code 1 if any check fails.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


X = "a" * 64
CONV = "dm_" + X[:4]
MID = "3fa9c1" + "0" * 58          # a message the person sent
OTHER = "3fa9c2" + "1" * 58        # same first 5 hex, different 6th


class FakeDaemon:
    def __init__(self):
        self.calls = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                me.calls.append((self.path, body))
                if self.path == "/dm":
                    return self._json(200, {"conversationId": CONV, "peerInboxId": body["inboxId"]})
                self._json(200, {"id": f"{len(me.calls):06x}" + "e" * 58})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sends(self):
        return [b for p, b in self.calls if p == "/send"]


D = FakeDaemon()
DATA = tempfile.mkdtemp(prefix="gpette_tags_")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write(f'''[server]
data_dir = "{DATA.replace(chr(92), "/")}"

[daemon]
url = "{D.url}"

[senders]
resolver = ""

[gate]
kind = "none"

[profiles.main]
members = ["owner"]
gate = "open"
send = true
''')
os.environ["GPETTE_CONFIG"] = CONFIG

from modules import msg_tags, profiles, reply_block, send_block  # noqa: E402

# --- tags --------------------------------------------------------------------------------
line = msg_tags.incoming_line(CONV, {"id": MID}, "how's it going?")
check("incoming text gets its tag", line == "[m:3fa9c1] how's it going?", line)
check("the tag resolves in its chat", msg_tags.resolve(CONV, "m:3fa9c1") == (MID, None))
check("not in another chat", msg_tags.resolve("other_conv", "m:3fa9c1")[0] is None)
check("an unknown tag says why", "not a message in this chat" in (msg_tags.resolve(CONV, "m:abcdef")[1] or ""))
check("a full id passes as is", msg_tags.resolve(CONV, "c" * 64) == ("c" * 64, None))
check("garbage is refused", msg_tags.resolve(CONV, "hello")[0] is None)

reply = msg_tags.incoming_line(CONV, {"id": "7b20aa" + "2" * 58, "replyTo": MID}, "fine, you?")
check("a reply names what it answers",
      reply == '[m:7b20aa, reply to m:3fa9c1 "how\'s it going?"] fine, you?', reply)
react = msg_tags.incoming_line(CONV, {"id": "9c01de" + "3" * 58,
                                      "reaction": {"reference": MID, "action": "added"}}, "🫡")
check("a reaction becomes a line", react == '[m:9c01de] [reaction 🫡 added to m:3fa9c1 "how\'s it going?"]', react)

msg_tags.remember(CONV, OTHER, "x")
check("an ambiguous short tag asks for more", "matches 2" in (msg_tags.resolve(CONV, "m:3fa9c")[1] or ""))
check("six hex still picks one", msg_tags.resolve(CONV, "m:3fa9c1")[0] == MID)

# --- the send block: reaction and reply ---------------------------------------------------
b = send_block.parse(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nTO: m:3fa9c1,\nREACTION: 🫡}}")
check("a reaction block", b == [{"kind": "inbox", "target": X, "to": "m:3fa9c1", "reaction": "🫡"}], b)
b = send_block.parse(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nTO: m:3fa9c1,\nMESSAGE: on it}}")
check("a reply block", b == [{"kind": "inbox", "target": X, "to": "m:3fa9c1", "message": "on it"}], b)
b = send_block.parse(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nREACTION: 🫡}}")
check("a reaction without TO is an error", "error" in b[0], b)
b = send_block.parse('[SEND_XMTP_MESSAGE]\n{"inboxId": "%s", "to": "m:3fa9c1", "reaction": "👍"}' % X)
check("json reaction", b and b[0].get("reaction") == "👍" and b[0].get("to") == "m:3fa9c1", b)

main = profiles.Profile(name="main", send=True) if hasattr(profiles, "Profile") else None
p = main or profiles.resolve_profile(CONV, "owner", "dm")
send_block.start_turn("turn1", items=1)
out = send_block.run(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nTO: m:3fa9c1,\nREACTION: 🫡}}", p, "turn1")
s = D.sends()[-1]
check("reaction sent with the full id", s.get("contentType") == "reaction" and s.get("reference") == MID
      and s.get("content") == "🫡", s)
check("result says Reaction sent", "Reaction sent" in out, out)
out = send_block.run(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nTO: m:3fa9c1,\nMESSAGE: on it}}", p, "turn1")
s = D.sends()[-1]
check("reply sent", s.get("contentType") == "reply" and s.get("reference") == MID and s.get("content") == "on it", s)
out = send_block.run(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nTO: m:ffffff,\nREACTION: 🫡}}", p, "turn1")
check("an unknown tag is not sent", "NOT sent" in out and "not a message in this chat" in out, out)
before = len(D.sends())
out = send_block.run(f"[SEND_XMTP_MESSAGE]\n{{INBOXID: {X},\nMESSAGE: plain}}", p, "turn1")
check("a plain message still works", len(D.sends()) == before + 1 and "contentType" not in D.sends()[-1], D.sends()[-1])

# --- [REPLY] ------------------------------------------------------------------------------
check("no block, no reply", not reply_block.has_block("I could use [REPLY] here."))
r = reply_block.parse("[REPLY]\n{TO: m:3fa9c1,\nMESSAGE: yes, done}")
check("the reply block", r == {"to": "m:3fa9c1", "message": "yes, done"}, r)
r = reply_block.parse("[REPLY]\n{MESSAGE: lost tag}")
check("no TO keeps the text to deliver", r.get("error") and r.get("message") == "lost tag", r)

from modules.chat_controller import ChatController  # noqa: E402
fr = {"content": "[REPLY]\n{TO: m:3fa9c1,\nMESSAGE: yes, done}"}
ChatController._apply_reply_block(None, fr, CONV)
check("final answer becomes a reply", fr.get("contentType") == "reply" and fr.get("reference") == MID
      and fr["content"]["value"] == "yes, done", fr)
fr = {"content": "[REPLY]\n{TO: m:000000,\nMESSAGE: still said}"}
ChatController._apply_reply_block(None, fr, CONV)
check("a bad tag delivers plain text", "contentType" not in fr and fr["content"]["value"] == "still said", fr)

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
