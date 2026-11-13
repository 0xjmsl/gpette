"""/menu and the per-profile error replies, checked without a model or XMTP.

    python tests/test_admin_menu.py

Exit code 1 if any check fails.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("GPETTE_CONFIG", os.path.join(tempfile.mkdtemp(), "none.toml"))

from config import Config  # noqa: E402

Config.DATA_DIR = tempfile.mkdtemp(prefix="gpette_menu_")
Config.PROFILES = {
    "me": {"members": ["owner"], "admin": True, "model": "fable"},
    "public": {"members": ["stranger"], "error_reply": "Back soon."},
}

from modules import admin_menu  # noqa: E402
from modules.profiles import resolve_profile  # noqa: E402

failures = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)


def msg(text, sender="owner", ctype="text", intent=None):
    m = {"conversationId": "c1", "senderInboxId": sender, "contentType": ctype,
         "content": {"type": "text", "value": text}}
    if intent:
        m["intent"] = intent
    return m


me = resolve_profile("c1", "owner", "dm")
pub = resolve_profile("c2", "stranger", "dm")
check("admin profile describes errors", me.admin and me.on_error == "describe")
check("public profile is generic", not pub.admin and pub.on_error == "generic")

# /menu: buttons, only for admin
out, consumed, _ = admin_menu.handle(msg("/menu"), me)
check("/menu sends one actions message", consumed and out[0]["contentType"] == "actions")
labels = [a["label"] for a in out[0]["actions"]["actions"]]
check("main menu buttons", labels == ["Status", "Model", "Session", "Timers", "Errors", "Help"])
out, consumed, _ = admin_menu.handle(msg("/menu", sender="stranger"), pub)
check("/menu is plain text on a public profile", not consumed and not out)

# Model switch by button, survives a 'restart' (state reloaded from disk)
out, consumed, _ = admin_menu.handle(msg("model:sonnet", ctype="intent",
                                         intent={"id": "x", "actionId": "model:sonnet"}), me)
check("model button confirms", consumed and "sonnet" in out[0]["content"]["value"])
admin_menu._state = None
check("override survives a restart", resolve_profile("c1", "owner", "dm").model == "sonnet")
admin_menu.handle(msg("/model default"), me)
check("/model default clears it", resolve_profile("c1", "owner", "dm").model == "fable")

# A typed number answers the last menu, once
admin_menu.handle(msg("/menu"), me)
out, consumed, _ = admin_menu.handle(msg("6"), me)
check("typed 6 = Help", consumed and "Shortcuts" in out[0]["content"]["value"])
out, consumed, _ = admin_menu.handle(msg("6"), me)
check("a second number is chat again", not consumed)

# New session goes through the normal /reset path
_, consumed, replacement = admin_menu.handle(
    msg("session:new", ctype="intent", intent={"id": "x", "actionId": "session:new"}), me)
check("new session -> /reset", consumed and replacement == "/reset")

# Timers pause
admin_menu.handle(msg("t", ctype="intent", intent={"id": "x", "actionId": "timers:pause"}), me)
check("timers paused", admin_menu.timers_paused())
admin_menu.handle(msg("t", ctype="intent", intent={"id": "x", "actionId": "timers:resume"}), me)
check("timers resumed", not admin_menu.timers_paused())

# Errors
limit = "claude exited 1: 429 model_requires_usage_credits You've hit your limit"
check("limit is classified", admin_menu.classify(limit) == "limit")
check("a limit does not drop the session", not admin_menu.is_stale_session(limit))
check("a missing session does", admin_menu.is_stale_session("No conversation found with session ID"))
check("public sees its error_reply",
      admin_menu.error_reply(pub, "c2", limit, is_timer=False) == "Back soon.")
first = admin_menu.error_reply(me, "c1", limit, is_timer=True)
check("first timer failure is described", first and "Usage limit" in first and "/menu" in first)
check("the next ones are quiet", admin_menu.error_reply(me, "c1", limit, is_timer=True) is None)
back = admin_menu.note_success(me, "c1", {"total_cost_usd": 0.5, "usage": {"input_tokens": 10}})
check("first success says back", back and "2 timer fire(s)" in back)
check("then nothing", admin_menu.note_success(me, "c1", {}) is None)
out, _, _ = admin_menu.handle(msg("/status"), me)
check("status shows the cost", "$0.50" in out[0]["content"]["value"])

# Every button answers in a shape server.sendMessage takes: a menu
# (contentType "actions") or text as content = {type: "text", value: str}.
# A bare string as content made sendMessage raise on 2026-11-10, and the
# daemon retried that press forever.
def sendable(m):
    if m.get("contentType") == "actions":
        return bool(m.get("actions", {}).get("actions"))
    c = m.get("content")
    return isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("value"), str)


for action in ("status", "model", "model:opus", "model:default", "session", "timers",
               "timers:pause", "timers:resume", "errors", "help", "menu", "gone"):
    out, consumed, _ = admin_menu.handle(
        msg(action, ctype="intent", intent={"id": "x", "actionId": action}), me)
    check(f"button {action} answers in a sendable shape",
          consumed and len(out) == 1 and sendable(out[0]))
for typed in ("/status", "/model", "/model opus", "/model default", "/menu"):
    out, consumed, _ = admin_menu.handle(msg(typed), me)
    check(f"{typed} answers in a sendable shape", consumed and len(out) == 1 and sendable(out[0]))

# Cost: Claude Code's total_cost_usd is the session's running total, and usage
# is summed over the run's model calls. Three runs of one session at totals
# 1.00, 1.20, 1.50 cost 1.00 + 0.20 + 0.30; a new session starts over.
admin_menu._load()["costs"].clear()
def run(total, session, turns=3, read=600_000):
    admin_menu.note_success(me, "c1", {"total_cost_usd": total, "session_id": session, "num_turns": turns,
                                       "usage": {"cache_read_input_tokens": read}})
    return admin_menu._load()["last_turn"]["c1"]
check("first run of a session costs its total", abs(run(1.00, "s1")["cost"] - 1.00) < 1e-6)
check("a resumed run costs the difference", abs(run(1.20, "s1")["cost"] - 0.20) < 1e-6)
last = run(1.50, "s1")
check("and again", abs(last["cost"] - 0.30) < 1e-6)
check("context is tokens per model call", last["context"] == 200_000)
check("the day adds runs, not totals", abs(sum(admin_menu._load()["costs"].values()) - 1.50) < 1e-6)
check("a new session starts over", abs(run(0.25, "s2")["cost"] - 0.25) < 1e-6)
out, _, _ = admin_menu.handle(msg("/status"), me)
check("status shows the run, the context and the session total",
      "Last turn: $0.25" in out[0]["content"]["value"] and "200,000 tokens of context" in out[0]["content"]["value"])

# A run that is not kept (a timer with skip_session) runs in a session of its
# own: its cost counts, the conversation's last turn and session stay.
before = dict(admin_menu._load()["last_turn"]["c1"])
day = sum(admin_menu._load()["costs"].values())
admin_menu.note_success(me, "c1", {"total_cost_usd": 0.40, "session_id": "t1", "num_turns": 2,
                                   "usage": {"cache_read_input_tokens": 50_000}}, throwaway=True)
check("a run that is not kept still costs", abs(sum(admin_menu._load()["costs"].values()) - day - 0.40) < 1e-6)
check("and leaves the last turn alone", admin_menu._load()["last_turn"]["c1"] == before
      and "t1" not in admin_menu._load()["sessions"])
check("the kept session goes on from its own total", abs(run(0.35, "s2")["cost"] - 0.10) < 1e-6)

# The size of a session is the LAST model call of the run (usage.iterations):
# what it read, fresh and cached, plus what it wrote.
admin_menu.note_success(me, "c1", {
    "total_cost_usd": 0.50, "session_id": "s2", "num_turns": 5,
    "usage": {"cache_read_input_tokens": 1_500_000, "output_tokens": 4_000, "iterations": [
        {"input_tokens": 20, "cache_read_input_tokens": 300_000,
         "cache_creation_input_tokens": 1_000, "output_tokens": 480}]}})
check("context is the last model call when the CLI reports it",
      admin_menu._load()["last_turn"]["c1"]["context"] == 301_500 and admin_menu.session_context("s2") == 301_500)
check("an unknown session has no size", admin_menu.session_context("nope") == 0)
day = sum(admin_menu._load()["costs"].values())
admin_menu.note_cost(0.003)
admin_menu.note_cost(None)
check("a compaction call is counted in the day", abs(sum(admin_menu._load()["costs"].values()) - day - 0.003) < 1e-6)

# A state file from before sessions were tracked: their totals come from the
# last turns, so the first run after the upgrade costs the difference.
admin_menu._load().pop("sessions")
admin_menu._save()
admin_menu._state = None
check("an older state file is carried over", admin_menu.session_context("s2") == 301_500
      and abs(run(0.75, "s2")["cost"] - 0.25) < 1e-6)

# Session menu: both buttons go on to the chat as a command.
out, _, _ = admin_menu.handle(msg("session", ctype="intent", intent={"id": "x", "actionId": "session"}), me)
check("session menu buttons", [a["label"] for a in out[0]["actions"]["actions"]]
      == ["Compact now", "New session", "Back"])
out, consumed, replacement = admin_menu.handle(
    msg("session:compact", ctype="intent", intent={"id": "x", "actionId": "session:compact"}), me)
check("compact now -> /compact", consumed and not out and replacement == "/compact")
check("/compact and /reset typed are chat, not menu",
      admin_menu.menu_action(msg("/compact"), me) is None and admin_menu.menu_action(msg("/reset"), me) is None)


class SummaryDb:
    def get_summary(self, key):
        return {"summary": "one two three four", "through_ts": 1, "updated_at": 0} if key == "c1" else None

    def get_timers_for_conversation(self, cid):
        return []


out, _, _ = admin_menu.handle(msg("/status"), me, SummaryDb())
check("status shows the summary", "Summary: 4 words" in out[0]["content"]["value"])

# One definition of "this is menu input": what handle consumes is exactly
# what menu_action names.
for text, ctype, intent in (("/menu", "text", None), ("menu", "text", None), ("/status", "text", None),
                            ("/model", "text", None), ("/model opus", "text", None),
                            ("hello there", "text", None), ("7", "text", None),
                            ("x", "intent", {"id": "x", "actionId": "help"})):
    m = msg(text, ctype=ctype, intent=intent)
    named = admin_menu.menu_action(m, me) is not None
    _, consumed, _ = admin_menu.handle(m, me)
    check(f"menu_action and handle agree on {text!r} ({ctype})", named == consumed)
check("a public profile has no menu actions", admin_menu.menu_action(msg("/menu", sender="stranger"), pub) is None)

# When the menu raises, the press is dropped and the user is told; ordinary
# chat gets no notice and goes on to the model.
boom = AttributeError("'str' object has no attribute 'get'")
n = admin_menu.failure_notice(msg("x", ctype="intent", intent={"id": "x", "actionId": "status"}), me, boom)
check("a failed press gets a sendable notice", n and sendable(n) and n["conversationId"] == "c1")
check("the notice names the press and the error",
      n and '"status"' in n["content"]["value"] and "AttributeError" in n["content"]["value"])
check("a failed /status gets a notice", admin_menu.failure_notice(msg("/status"), me, boom) is not None)
check("ordinary chat gets no notice", admin_menu.failure_notice(msg("hello there"), me, boom) is None)
check("a public profile gets no notice", admin_menu.failure_notice(msg("/menu", sender="stranger"), pub, boom) is None)

# The real thing: _act raises inside handle; the caller's except asks for the notice.
real_act = admin_menu._act
def broken_act(*a, **k):
    raise RuntimeError("menu is broken")
admin_menu._act = broken_act
try:
    for m in (msg("/menu"), msg("/status"), msg("x", ctype="intent", intent={"id": "x", "actionId": "help"})):
        try:
            admin_menu.handle(m, me)
            check("a broken menu raises out of handle", False)
        except RuntimeError as e:
            n = admin_menu.failure_notice(m, me, e)
            check("a broken menu still answers with the notice", n and sendable(n) and "menu is broken" in n["content"]["value"])
    _, consumed, _ = admin_menu.handle(msg("hello there"), me)
    check("a broken menu does not touch ordinary chat", not consumed)
finally:
    admin_menu._act = real_act

# A typed number: handle closes the menu before acting, so the notice must
# still see it as a press.
admin_menu.handle(msg("/menu"), me)
admin_menu._act = broken_act
try:
    m = msg("1")
    try:
        admin_menu.handle(m, me)
        check("a typed number on a broken menu raises", False)
    except RuntimeError as e:
        n = admin_menu.failure_notice(m, me, e)
        check("a typed number on a broken menu gets the notice", n and sendable(n))
finally:
    admin_menu._act = real_act

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
