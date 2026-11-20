"""Several inboxes, one gpette ([inboxes], modules/inboxes.py). No model, no XMTP:
three fake daemons answer /health and record /send.

    python tests/test_inboxes.py

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


class FakeDaemon:
    """/health answers an inbox id; /send records what it got."""

    def __init__(self, inbox_id):
        self.inbox_id, self.sent = inbox_id, []
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
                self._json(200, {"inboxId": me.inbox_id}) if self.path == "/health" else self._json(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                me.sent.append(json.loads(self.rfile.read(n) or b"{}"))
                self._json(200, {"id": f"m{len(me.sent)}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


A, B, BASE = FakeDaemon("aaaa1111"), FakeDaemon("bbbb2222"), FakeDaemon("base0000")

DATA = tempfile.mkdtemp(prefix="gpette_inboxes_")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write(f'''[server]
data_dir = "{DATA.replace(chr(92), "/")}"

[daemon]
url = "{BASE.url}"

[gate]
kind = "none"

[inboxes.alpha]
url = "{A.url}"

[inboxes.beta]
url = "{B.url}"

[profiles.alpha]
inbox = "alpha"
members = ["owner"]
gate = "open"

[profiles.beta]
inbox = "beta"
gate = "open"

[profiles.owner_anywhere]
members = ["owner", "friend"]
gate = "open"

[profiles.club]
inbox = "alpha"
members = ["group_club"]
gate = "open"
''')
os.environ["GPETTE_CONFIG"] = CONFIG

from config import Config  # noqa: E402
from modules import inboxes, profiles  # noqa: E402
from server.xmtp_http_bridge import send_via_daemon  # noqa: E402


def text(conv):
    return {"conversationId": conv, "content": "hi", "contentType": "text"}


# --- the config check -----------------------------------------------------------
inboxes.check_config()
check("a good config passes the boot check", True)
Config.PROFILES["broken"] = {"inbox": "gamma"}
try:
    inboxes.check_config()
    check("a profile bound to an inbox that is not configured is refused", False)
except ValueError as e:
    check("a profile bound to an inbox that is not configured is refused", "gamma" in str(e), e)
del Config.PROFILES["broken"]
Config.INBOXES["nourl"] = {}
try:
    inboxes.check_config()
    check("an inbox with no url is refused", False)
except ValueError as e:
    check("an inbox with no url is refused", "nourl" in str(e), e)
del Config.INBOXES["nourl"]

# --- inbox ids come from /health --------------------------------------------------
check("inbox ids are read from each daemon's /health",
      inboxes.inbox_id_of("alpha") == "aaaa1111" and inboxes.inbox_id_of("beta") == "bbbb2222")
check("an id maps back to its name", inboxes.name_of("0xBBBB2222") == "beta")

# --- incoming: the receiving inbox picks the profile ------------------------------
p = profiles.resolve_profile("conv_a", "owner", "dm", "aaaa1111")
check("the owner writing to inbox alpha gets profile alpha", p.name == "alpha", p.name)
p = profiles.resolve_profile("conv_b", "owner", "dm", "bbbb2222")
check("the SAME owner writing to inbox beta gets profile beta", p.name == "beta", p.name)
p = profiles.resolve_profile("conv_s", "stranger", "dm", "bbbb2222")
check("a bound profile with no members answers anyone at its inbox", p.name == "beta", p.name)
p = profiles.resolve_profile("conv_f", "friend", "dm", "aaaa1111")
check("someone alpha does not list falls to the profiles bound to no inbox",
      p.name == "owner_anywhere", p.name)
p = profiles.resolve_profile("conv_x", "stranger", "dm", "aaaa1111")
check("a stranger at alpha with no catch-all gets the default profile", p.name == "default", p.name)
p = profiles.resolve_profile("group_club", "someone", "group", "aaaa1111")
check("a group at alpha is matched by its id among alpha's profiles", p.name == "club", p.name)
p = profiles.resolve_profile("conv_old", "owner", "dm")
check("an envelope with no inboxId (an older daemon) matches as before", p.name == "alpha", p.name)

# --- outgoing: the conversation's inbox picks the daemon ---------------------------
send_via_daemon(text("conv_a"))
send_via_daemon(text("conv_b"))
send_via_daemon(text("conv_unknown"))
check("a reply in alpha's chat leaves through alpha's daemon",
      [m["conversationId"] for m in A.sent] == ["conv_a"], A.sent)
check("a reply in beta's chat leaves through beta's daemon",
      [m["conversationId"] for m in B.sent] == ["conv_b"], B.sent)
check("a conversation gpette never saw leaves through [daemon] url",
      [m["conversationId"] for m in BASE.sent] == ["conv_unknown"], BASE.sent)

# --- a restart: the conversation's inbox is remembered on disk --------------------
inboxes._conv_inbox = None
profiles._last.clear()
check("after a restart conv_b still belongs to beta", inboxes.of_conversation("conv_b") == "beta")
p = profiles.profile_for_conversation("conv_b", "dm")
check("after a restart a tool result in conv_b runs in profile beta", p.name == "beta", p.name)

# --- timers -------------------------------------------------------------------------
profiles._last.clear()
p, why = profiles.profile_for_timer({"conversation_id": "conv_new", "profile": "beta", "owner_inbox": "owner"}, False)
check("a timer of profile beta may fire in a chat gpette never saw", p is not None and p.name == "beta", why)
send_via_daemon(text("conv_new"))
check("its first message leaves through beta's daemon (a DM the bot opened itself)",
      B.sent[-1]["conversationId"] == "conv_new", B.sent[-1])
p, why = profiles.profile_for_timer({"conversation_id": "conv_b", "profile": "alpha", "owner_inbox": "owner"}, False)
check("a timer of profile alpha may not fire in beta's chat", p is None and "beta" in (why or ""), why)
p, why = profiles.profile_for_timer({"conversation_id": "conv_a", "profile": "alpha", "owner_inbox": "owner"}, False)
check("a timer of profile alpha fires in alpha's chat", p is not None and p.name == "alpha", why)

# --- one group, two of our inboxes in it: each bot has its own copy of the chat ------
check("a group id seen by inbox beta is split back", inboxes.split("group_club@beta") == ("group_club", "beta"))
check("an '@' that names no inbox is left alone", inboxes.split("weird@nobody") == ("weird@nobody", None))
p = profiles.resolve_profile("group_club@alpha", "bbbb2222", "group", "aaaa1111")
check("in the shared group, alpha's copy runs in alpha's group profile", p.name == "club", p.name)
p = profiles.resolve_profile("group_club@beta", "aaaa1111", "group", "bbbb2222")
check("beta answers everyone who DMs it, but not a group nobody listed", p.name == "group", p.name)
Config.INBOXES["beta"]["nothing_matched_groups"] = True
p = profiles.resolve_profile("group_club@beta", "aaaa1111", "group", "bbbb2222")
check("with nothing_matched_groups, beta's copy of the group runs in beta's profile", p.name == "beta", p.name)
before_a, before_b = len(A.sent), len(B.sent)
send_via_daemon(text("group_club@alpha"))
send_via_daemon(text("group_club@beta"))
check("alpha's answer in the group leaves through alpha, as the plain group id",
      len(A.sent) == before_a + 1 and A.sent[-1]["conversationId"] == "group_club", A.sent[-1:])
check("beta's answer in the same group leaves through beta, as the plain group id",
      len(B.sent) == before_b + 1 and B.sent[-1]["conversationId"] == "group_club", B.sent[-1:])
profiles._last.clear()
p, why = profiles.profile_for_timer({"conversation_id": "group_club@beta", "profile": "club", "owner_inbox": "owner"}, True)
check("a timer of alpha's group profile may not fire in beta's copy of the group", p is None, why)

# --- no [inboxes]: everything as before -----------------------------------------------
saved = dict(Config.INBOXES)
Config.INBOXES.clear()
check("with no [inboxes] every message leaves through [daemon] url",
      inboxes.daemon_url_for("conv_a") == BASE.url)
p = profiles.resolve_profile("conv_z", "friend", "dm", "aaaa1111")
check("with no [inboxes] the inboxId is ignored", p.name == "owner_anywhere", p.name)
Config.INBOXES.update(saved)

for d in (A, B, BASE):
    d.server.shutdown()
print(f"\n{'ALL OK' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
