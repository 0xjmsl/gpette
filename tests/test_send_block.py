"""[SEND_XMTP_MESSAGE] (modules/send_block.py): the block, its budget, and the round
trip through the thread controller and the chat controller. A fake daemon answers
/dm and /send; the model is a script. No Claude, no XMTP.

    python tests/test_send_block.py

Exit code 1 if any check fails.
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


X = "a" * 64            # an inbox we write to
Y = "b" * 64
BROKEN = "f" * 64       # the daemon refuses to send to this one


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

            def do_GET(self):
                self._json(200, {"inboxId": "self0000"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                me.calls.append((self.path, body))
                if self.path == "/dm":
                    return self._json(200, {"conversationId": "dm_" + body["inboxId"][:4],
                                            "peerInboxId": body["inboxId"]})
                if self.path == "/send" and body.get("conversationId") == "dm_" + BROKEN[:4]:
                    return self._json(502, {"error": "xmtp refused"})
                self._json(200, {"id": f"m{len(me.calls)}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sends(self):
        return [b for p, b in self.calls if p == "/send"]


D = FakeDaemon()
DATA = tempfile.mkdtemp(prefix="gpette_send_")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write(f'''[server]
data_dir = "{DATA.replace(chr(92), "/")}"
stack_secs = 0.3

[daemon]
url = "{D.url}"

[llm]
provider = "claude-code"

[senders]
resolver = ""

[gate]
kind = "none"

[profiles.main]
members = ["owner"]
gate = "open"
send = true
public_profile = "desk"

[profiles.public]
members = ["guest"]
gate = "open"

[profiles.desk]
gate = "password"
workdir = "{DATA.replace(chr(92), "/")}/desk"
per_inbox = true
allowed_tools = ["Read"]

[profiles.club]
members = ["group_club"]
gate = "open"
''')
os.environ["GPETTE_CONFIG"] = CONFIG

from config import Config  # noqa: E402
from modules import profiles, send_block  # noqa: E402
from modules.chat_controller import ChatController  # noqa: E402
from modules.function_registry import FunctionRegistry  # noqa: E402
from modules.thread_controller import ThreadController  # noqa: E402


def block(target, message, key="INBOXID"):
    return f"[SEND_XMTP_MESSAGE]\n{{{key}: {target},\nMESSAGE: {message}}}"


# --- reading the block ------------------------------------------------------------------
check("an answer with no block is left alone", send_block.parse("just an answer") == [])
b = send_block.parse(block(X, "hello there, how are you?"))
check("the block as the design writes it",
      b == [{"kind": "inbox", "target": X, "message": "hello there, how are you?"}], b)
b = send_block.parse("I will tell them.\n\n" + block(X, "line one\nline two, with a comma {and braces}") + "\n")
check("text around it, and a message with newlines, commas and braces",
      len(b) == 1 and b[0]["message"] == "line one\nline two, with a comma {and braces}", b)
b = send_block.parse('[SEND_XMTP_MESSAGE]\n{"inboxId": "%s", "message": "as json"}' % X)
check("a JSON body works too", b == [{"kind": "inbox", "target": X, "message": "as json"}], b)
b = send_block.parse(block("group_club", "to the group", key="GROUPID"))
check("GROUPID makes it a group", b and b[0]["kind"] == "group" and b[0]["target"] == "group_club", b)
b = send_block.parse("[SEND XMTP MESSAGE]\n{INBOXID: %s,\nMESSAGE: spaces in the token}" % X)
check("the token written with spaces is caught too", len(b) == 1 and "error" not in b[0], b)
b = send_block.parse(block(X, "one") + "\n" + block(Y, "two"))
check("two blocks are both read (and refused as a pair when run)", [x.get("message") for x in b] == ["one", "two"], b)
check("a block with no id is an error, not a guess",
      "error" in send_block.parse("[SEND_XMTP_MESSAGE]\n{MESSAGE: to whom?}")[0])
check("a block with no body is an error", "error" in send_block.parse("[SEND_XMTP_MESSAGE]\nhello")[0])
check("the tag named inside a sentence is not a block",
      not send_block.has_block("I did not use the `[SEND_XMTP_MESSAGE]` block for that one."))
check("the tag alone on a line, with markdown around it, is one",
      send_block.has_block("**[SEND_XMTP_MESSAGE]**\n{INBOXID: " + "a" * 64 + ",\nMESSAGE: hi}"))
check("strip() leaves the text before the block", send_block.strip("Hi.\n" + block(X, "m")) == "Hi.")

# --- the budget ----------------------------------------------------------------------------
send_block.start_turn("t1", 1)
check("one natural prompt gives 3 sends", send_block.left("t1") == 3)
send_block.start_turn("t1", 2)
check("two stacked items give 6", send_block.left("t1") == 6)
send_block.start_turn("t1", 9)
check("never more than 15", send_block.left("t1") == 15)
check("a chat with no turn started has none", send_block.left("never") == 0)

# --- the round trip ------------------------------------------------------------------------
tc = ThreadController()
registry = FunctionRegistry(tc)
cc = ChatController(service="claude-code", thread_controller=tc, function_registry=registry)
script, heard, out = [], [], []


def fake_model(message, history, system_prompt, tools=None, skip_session=False, is_group=False, profile=None):
    content = message.get("content")
    text = content.get("value") if isinstance(content, dict) else content
    heard.append({"role": message.get("role"), "text": text, "profile": profile.name if profile else None})
    answer = script.pop(0) if script else "(script empty)"
    return {"content": {"type": "text", "value": answer}, "conversationId": message.get("conversationId"),
            "tool_calls": None, "token_count": 0}


cc.service.send_message = fake_model
tc.register_handler("FunctionRegistry", registry.receive_Message_FunctionRegistryr)
tc.register_handler("ChatController", cc.receive_Message_ChatController)
tc.register_handler("SocketIO", lambda m: out.append(m))
tc.start()


def user(conv, text, sender):
    return {"role": "user", "conversationId": conv, "senderInboxId": sender, "source": "SocketIO",
            "contentType": "text", "kind": "dm", "timestamp": int(time.time() * 1000),
            "content": {"type": "text", "value": text}}


def turn(conv, sender, texts, answers, wait_heard):
    """Send `texts` (stacked when several), let the scripted model answer, return what
    went to the chat."""
    del heard[:], out[:], D.calls[:]
    script[:] = answers
    for t in texts:
        tc.add_message(user(conv, t, sender))
        time.sleep(0.05)
    end = time.time() + 15
    while (len(heard) < wait_heard or len(out) < 1) and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.3)
    return [m["content"]["value"] for m in out if m.get("role") == "assistant"]


def results():
    return [h["text"] for h in heard if h["role"] == "tool"]


# 1. main writes to someone and carries on
sent = turn("dm_owner", "owner", ["tell X we are live"],
            ["On it.\n" + block(X, "We are live."), "Done, I told them."], wait_heard=2)
check("the daemon opened the DM and sent the message",
      [p for p, _ in D.calls] == ["/dm", "/send"] and D.sends()[0] == {"conversationId": "dm_aaaa", "content": "We are live."},
      D.calls)
check("the answer that held the block did not go to the chat", sent == ["Done, I told them."], sent)
check("the model got the result and the sends left",
      results() and f"Message sent to inbox {X}." in results()[0] and "2 sends left" in results()[0], results())
check("the result ran in the same profile", heard[-1]["profile"] == "main", heard)
check("a first message to someone says their public folder was made",
      f"New public folder created for inbox {X}." in results()[0], results())
check("and the folder is there", os.path.isdir(os.path.join(DATA, "desk", X)))
p = profiles.resolve_profile("dm_aaaa", X, "dm")
check("when they answer, the desk profile takes it, with no password",
      p.name == "desk" and p.gate == "open", (p.name, p.gate))
stored = tc.get_chat_history("dm_aaaa", limit=5)
check("the sent message is kept in the other chat's history",
      any((m.get("content") or {}).get("value") == "We are live." for m in stored), stored)

# 2. a profile without the switch
sent = turn("dm_guest", "guest", ["message X for me"],
            [block(X, "spam"), "I can't do that from here."], wait_heard=2)
check("without send = true nothing leaves", D.calls == [], D.calls)
check("the model is told so", results() and "may not send" in results()[0], results())
check("and its next answer reaches the person", sent == ["I can't do that from here."], sent)

# 3. one block per answer; the budget runs out at the fourth send
sent = turn("dm_owner", "owner", ["tell two people at once"],
            [block(X, "one") + "\n" + block(Y, "two"), "ok"], wait_heard=2)
check("two blocks in one answer send nothing", D.calls == [], D.calls)
check("and the model is told to send one at a time",
      "Nothing sent: 2 blocks" in results()[0] and "3 sends left" in results()[0], results())

sent = turn("dm_owner", "owner", ["tell four people"],
            [block(X, "n0"), block(X, "n1"), block(X, "n2"), block(X, "n3"), "Three went out."], wait_heard=5)
check("three sends go out, one per answer", len(D.sends()) == 3, D.sends())
check("a second message to the same person makes no new folder", "New public folder" not in results()[0], results())
check("the count went 2, 1, 0", [r.split(". ")[-2] if False else r for r in results()][:3] and
      all(f"{n} send" in results()[i] for i, n in enumerate((2, 1, 0))), results())
check("the fourth is refused with the reason", "no sends left" in results()[3], results())
check("the person gets only the last answer", sent == ["Three went out."], sent)

# 4. a result does not refill the budget; a stacked turn gets 3 per message
sent = turn("dm_owner", "owner", ["first", "second"],
            [block(X, "a"), block(Y, "b"), "ok"], wait_heard=3)
check("two stacked messages: 6 sends, 5 left after the first", "5 sends left" in results()[0], results())
check("4 left after the second: the result in between added nothing", "4 sends left" in results()[1], results())

# 5. groups: only registered ones
sent = turn("dm_owner", "owner", ["report in the groups"],
            [block("group_club", "done", "GROUPID"), block("group_other", "done", "GROUPID"), "ok"],
            wait_heard=3)
check("a registered group gets the message, with no /dm",
      D.calls == [("/send", {"conversationId": "group_club", "content": "done"})], D.calls)
check("an unregistered group is refused", "group_other: that group is not registered" in results()[1], results())

# 6. failures answer, and cost nothing
sent = turn("dm_owner", "owner", ["try the broken one"],
            [block(BROKEN, "x"), block("nonsense", "y"), "ok"], wait_heard=3)
r = results()
check("a daemon refusal is reported with its reason", f"NOT sent to inbox {BROKEN}" in r[0] and "502" in r[0], r)
check("a target that is not an inbox id is reported", "not an inbox id" in r[1], r)
check("neither used a send", "3 sends left" in r[0] and "3 sends left" in r[1], r)

# 7. the turn can end in silence
sent = turn("dm_owner", "owner", ["tell X, say nothing to me"],
            [block(X, "psst"), "[NO_REPLY]"], wait_heard=2)
time.sleep(0.5)
check("[NO_REPLY] after a send delivers nothing", sent == [] and len(D.sends()) == 1, (sent, D.sends()))

# --- the boot check -----------------------------------------------------------------------
profiles.check_config()
check("this config passes the boot check", True)
Config.PROFILES["api_sender"] = {"members": ["z"], "provider": "openai", "send": True}
try:
    profiles.check_config()
    check("send = true on an API engine is refused at boot", False)
except ValueError as e:
    check("send = true on an API engine is refused at boot", "claude-code" in str(e), e)
del Config.PROFILES["api_sender"]

print(f"\n{'ALL OK' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
