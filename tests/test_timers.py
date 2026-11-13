"""Timers: which profile a fire runs in, and scripts/timer.py. No model, no XMTP.

    python tests/test_timers.py

Exit code 1 if any check fails.
"""
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DATA = tempfile.mkdtemp(prefix="gpette_timers_")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write('[server]\ndata_dir = "%s"\n\n[gate]\nkind = "none"\n\n'
            '[profiles.me]\nmembers = ["owner"]\ngate = "open"\nallowed_tools = ["Read", "Bash"]\n'
            % DATA.replace("\\", "/"))
os.environ["GPETTE_CONFIG"] = CONFIG

from models.conversation_db import ConversationDB  # noqa: E402
from modules import profiles  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


db = ConversationDB()
now = int(time.time() * 1000)


def store(conv, role, text, sender=None, ts=0):
    m = {"conversationId": conv, "role": role, "content": {"type": "text", "value": text}, "timestamp": now + ts}
    if sender:
        m["senderInboxId"] = sender
    db.store_message(m)


# --- a fire finds its profile after a restart, however long the chat went without a human
store("dm_owner", "user", "set up the hourly loop", sender="owner", ts=0)
for i in range(80):                       # 80 rows of timer fires and replies, no human message
    store("dm_owner", "assistant", f"quiet work {i}", ts=1 + i)
store("dm_guest", "user", "hello", sender="stranger", ts=0)

profiles._last.clear()                    # what a restart does
check("the store finds the last human sender past the recent window",
      db.last_human_sender("dm_owner") == "owner", db.last_human_sender("dm_owner"))
p = profiles.profile_for_conversation("dm_owner", "dm", db)
check("a fire in the owner's chat runs in the owner's profile after a restart", p.name == "me", p.name)
profiles._last.clear()
p = profiles.profile_for_conversation("dm_guest", "dm", db)
check("a fire in someone else's chat does not get the owner's profile", p.name != "me", p.name)
profiles._last.clear()
p = profiles.profile_for_conversation("dm_nobody", "dm", db)
check("a chat with no human message falls to the default profile", p.name != "me", p.name)

# --- scripts/timer.py
TIMER = os.path.join(REPO, "scripts", "timer.py")


def run(*args, conv=None):
    env = dict(os.environ)
    env.pop("GPETTE_CONVERSATION_ID", None)
    if conv:
        env["GPETTE_CONVERSATION_ID"] = conv
    r = subprocess.run([sys.executable, TIMER, *args], capture_output=True, text=True, encoding="utf-8", env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


code, out = run("set", "Hourly", "Do the quiet work", "--time", "30", "--type", "recurring", "--skip-session", conv="dm_owner")
check("set uses GPETTE_CONVERSATION_ID", code == 0 and "ID:" in out, out)
timer_id = out.rsplit("ID:", 1)[-1].strip() if "ID:" in out else ""
row = next((t for t in db.list_timers() if t["timer_id"] == timer_id), {})
check("the timer is in the owner's chat, recurring every 30 min, skip-session",
      row.get("conversation_id") == "dm_owner" and row.get("interval_seconds") == 1800 and bool(row.get("skip_session")), row)

code, out = run("set", "x", "y", "--time", "30")
check("set without a conversation is refused", code != 0 and "No conversation" in out, out)
code, out = run("set", "x", "y", "--time", "30", "--conv", "not_a_chat")
check("set in an unknown conversation is refused", code != 0 and "no conversation" in out, out)

code, out = run("list", conv="dm_owner")
check("list shows it", code == 0 and timer_id in out and "every 30min" in out and "[skip-session]" in out, out)
code, out = run("list", conv="dm_guest")
check("list for another chat does not", code == 0 and timer_id not in out, out)

code, out = run("edit", timer_id, "--time", "15", "--no-skip-session")
row = next((t for t in db.list_timers() if t["timer_id"] == timer_id), {})
check("edit changes the interval and skip-session", code == 0 and row.get("interval_seconds") == 900 and not row.get("skip_session"), row)

due_before = [t["timer_id"] for t in db.get_due_timers()]
db.update_timer(timer_id, fire_at=int(time.time()) - 5)
due = [t for t in db.get_due_timers() if t["timer_id"] == timer_id]
check("a due timer is picked up with its conversation", timer_id not in due_before and len(due) == 1 and due[0]["conversation_id"] == "dm_owner", due)

code, out = run("conversations")
check("conversations lists the chats and who a timer there runs as", code == 0 and "dm_owner" in out and "owner" in out and "dm_guest" in out, out)

code, out = run("cancel", timer_id)
row = next((t for t in db.list_timers() if t["timer_id"] == timer_id), {})
check("cancel", code == 0 and row.get("status") == "cancelled", row)

print()
if failures:
    print(f"{len(failures)} failed: " + "; ".join(failures))
    sys.exit(1)
print("all timer checks passed")
