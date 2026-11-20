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
            '[profiles.me]\nmembers = ["owner", "owner_phone"]\ngate = "open"\nallowed_tools = ["Read", "Bash"]\n'
            '[profiles.friends]\nmembers = ["friend"]\ngate = "open"\n'
            '[profiles.club]\nmembers = ["group_club"]\ngate = "open"\n'
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


# --- a tool result finds its profile after a restart, however long the chat went without a human
store("dm_owner", "user", "set up the hourly loop", sender="owner", ts=0)
for i in range(80):                       # 80 rows of timer fires and replies, no human message
    store("dm_owner", "assistant", f"quiet work {i}", ts=1 + i)
store("dm_guest", "user", "hello", sender="stranger", ts=0)

profiles._last.clear()                    # what a restart does
check("the store finds the last human sender past the recent window",
      db.last_human_sender("dm_owner") == "owner", db.last_human_sender("dm_owner"))
p = profiles.profile_for_conversation("dm_owner", "dm", db)
check("a tool result in the owner's chat runs in the owner's profile after a restart", p.name == "me", p.name)
profiles._last.clear()
p = profiles.profile_for_conversation("dm_guest", "dm", db)
check("in someone else's chat it does not get the owner's profile", p.name != "me", p.name)

# --- a timer fire runs as what the timer says, and only that
def fire(conv, profile, owner, group=False):
    profiles._last.clear()                # a restart: nothing remembered
    p, why = profiles.profile_for_timer({"conversation_id": conv, "profile": profile, "owner_inbox": owner}, group)
    return (p.name if p else None), why


store("dm_phone", "user", "from my other inbox", sender="owner_phone", ts=0)
store("dm_friend", "user", "hi", sender="friend", ts=0)
db.register_group_member("group_club", "owner")
db.register_group_member("group_loose", "owner")

check("a timer set by the owner fires as the owner's profile after a restart", fire("dm_owner", "me", "owner")[0] == "me")
check("the owner's second inbox has its own chat and its own timer", fire("dm_phone", "me", "owner_phone")[0] == "me")
name, why = fire("dm_owner", "", "owner")
check("a timer with no profile does not fire", name is None and "no profile" in why, why)
name, why = fire("dm_owner", "gone", "owner")
check("a timer whose profile left the config does not fire", name is None and "not in the config" in why, why)
name, why = fire("dm_guest", "me", "stranger")
check("a timer cannot run as a profile its inbox is not linked to", name is None and "belongs to profile 'default'" in why, why)
name, why = fire("dm_friend", "me", "friend")
check("nor as another person's profile", name is None and "belongs to profile 'friends'" in why, why)
name, why = fire("dm_owner", "me", "")
check("a DM timer with no inbox does not fire", name is None and "no inbox" in why, why)
check("a guest's timer fires as the default profile", fire("dm_guest", "default", "stranger")[0] == "default")
check("a timer in a listed group fires as the group's profile, whoever set it",
      fire("group_club", "club", "owner", group=True)[0] == "club")
name, why = fire("group_club", "me", "owner", group=True)
check("the owner does not get their own profile in a listed group", name is None and "belongs to profile 'club'" in why, why)
name, why = fire("group_loose", "me", "owner", group=True)
check("a timer in an unlisted group does not fire as its setter's profile (a group is never matched by a sender)",
      name is None and "belongs to profile 'group'" in why, why)
check("it fires as the group sandbox", fire("group_loose", "group", "owner", group=True)[0] == "group")

# two people with different profiles write in one chat: the timer stays its setter's
profiles.resolve_profile("dm_owner", "owner", "dm")
bound = profiles.timer_binding("dm_owner")
check("a timer set in a turn takes that turn's profile and inbox", bound == ("me", "owner"), bound)
profiles.resolve_profile("dm_owner", "friend", "dm")
check("and keeps them when someone else writes later", fire("dm_owner", *bound)[0] == "me")
try:
    db.create_timer("dm_owner", "x", "y", int(time.time()) + 60)
    check("the store refuses a timer without a profile", False)
except ValueError:
    check("the store refuses a timer without a profile", True)

# --- scripts/timer.py
TIMER = os.path.join(REPO, "scripts", "timer.py")


def run(*args, conv=None, profile=None, owner=None):
    env = dict(os.environ)
    for k, v in (("GPETTE_CONVERSATION_ID", conv), ("GPETTE_PROFILE", profile), ("GPETTE_TIMER_OWNER", owner)):
        env.pop(k, None)
        if v:
            env[k] = v
    r = subprocess.run([sys.executable, TIMER, *args], capture_output=True, text=True, encoding="utf-8", env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


code, out = run("set", "Hourly", "Do the quiet work", "--time", "30", "--type", "recurring", "--skip-session",
                conv="dm_owner", profile="me", owner="owner")
check("set uses the run's conversation, profile and inbox", code == 0 and "ID:" in out, out)
timer_id = out.rsplit("ID:", 1)[-1].strip() if "ID:" in out else ""
row = next((t for t in db.list_timers() if t["timer_id"] == timer_id), {})
check("the timer is in the owner's chat, recurring every 30 min, skip-session",
      row.get("conversation_id") == "dm_owner" and row.get("interval_seconds") == 1800 and bool(row.get("skip_session")), row)
check("and it holds its profile and inbox", row.get("profile") == "me" and row.get("owner_inbox") == "owner", row)

code, out = run("set", "x", "y", "--time", "30", conv="dm_owner")
check("set without a profile is refused", code != 0 and "No profile" in out, out)
code, out = run("set", "x", "y", "--time", "30", conv="dm_guest", profile="me", owner="stranger")
check("set as a profile the inbox is not linked to is refused", code != 0 and "would not fire" in out, out)
code, out = run("set", "x", "y", "--time", "30", "--conv", "dm_phone", "--profile", "me", "--owner", "owner_phone")
check("set from a terminal with --conv --profile --owner", code == 0 and "ID:" in out, out)

db._execute("INSERT INTO timers (timer_id, conversation_id, description, instructions, fire_at, created_at) "
            "VALUES ('oldtimer', 'dm_owner', 'made before the binding', 'x', ?, ?)", (int(time.time()) + 600, int(time.time())))
code, out = run("list", "--all")
check("list marks a timer with no profile", code == 0 and "UNBOUND" in out, out)
code, out = run("bind", "oldtimer", "--profile", "me", "--owner", "stranger")
check("bind refuses a profile the inbox is not linked to", code != 0 and "would not fire" in out, out)
code, out = run("bind", "oldtimer", "--profile", "me", "--owner", "owner")
row_old = next((t for t in db.list_timers() if t["timer_id"] == "oldtimer"), {})
check("bind writes the profile and inbox", code == 0 and row_old.get("profile") == "me" and row_old.get("owner_inbox") == "owner", out)

code, out = run("set", "x", "y", "--time", "30", profile="me", owner="owner")
check("set without a conversation is refused", code != 0 and "No conversation" in out, out)
code, out = run("set", "x", "y", "--time", "30", "--conv", "not_a_chat", "--profile", "me", "--owner", "owner")
check("set in an unknown conversation is refused", code != 0 and "no conversation" in out, out)

code, out = run("list", conv="dm_owner")
check("list shows it", code == 0 and timer_id in out and "every 30min" in out and "[skip-session]" in out, out)
code, out = run("list", conv="dm_guest")
check("list for another chat does not", code == 0 and timer_id not in out, out)

code, out = run("edit", timer_id, "--time", "15", "--no-skip-session")
row = next((t for t in db.list_timers() if t["timer_id"] == timer_id), {})
check("edit changes the interval and skip-session", code == 0 and row.get("interval_seconds") == 900 and not row.get("skip_session"), row)
check("edit keeps the profile and inbox", row.get("profile") == "me" and row.get("owner_inbox") == "owner", row)

due_before = [t["timer_id"] for t in db.get_due_timers()]
db.update_timer(timer_id, fire_at=int(time.time()) - 5)
due = [t for t in db.get_due_timers() if t["timer_id"] == timer_id]
check("a due timer is picked up with its conversation, profile and inbox",
      timer_id not in due_before and len(due) == 1 and due[0]["conversation_id"] == "dm_owner"
      and due[0].get("profile") == "me" and due[0].get("owner_inbox") == "owner", due)

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
