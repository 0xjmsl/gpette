"""Stacking (one turn for a chat's waiting messages) and group pacing
([NO_REPLY], group_reply_secs). No model, no XMTP.

    python tests/test_stacking.py

Exit code 1 if any check fails.
"""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DATA = tempfile.mkdtemp(prefix="gpette_stack_")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write(f'''[server]
data_dir = "{DATA.replace(chr(92), "/")}"
stack_secs = 0.4
group_reply_secs = 0

[gate]
kind = "none"

[profiles.slow]
members = ["patient"]
stack_secs = 1.2

[profiles.chatty]
members = ["group_chatty"]
group_reply_secs = 3600
''')
os.environ["GPETTE_CONFIG"] = CONFIG

from modules.thread_controller import ThreadController  # noqa: E402
from modules import profiles, group_pace  # noqa: E402
from modules.chat_controller import ChatController  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def user(conv, text, sender="alice", ctype="text"):
    return {"role": "user", "conversationId": conv, "senderInboxId": sender, "source": "SocketIO",
            "contentType": ctype, "kind": "dm", "timestamp": int(time.time() * 1000),
            "content": {"type": "text", "value": text}}


tc = ThreadController()
seen, busy = [], {"secs": 0}


def handler(message):
    fire = message.get("role") == "Proactive"
    batch = message.get("batch") or ([] if fire else [message])
    seen.append((message["conversationId"],
                 (["<timer>"] if fire else []) + [m["content"]["value"] for m in batch], time.monotonic()))
    time.sleep(busy["secs"])


tc.register_handler("ChatController", handler)
tc.start()


def wait_for(n, timeout=10):
    end = time.time() + timeout
    while len(seen) < n and time.time() < end:
        time.sleep(0.05)


# --- several quick messages become one turn; another chat and a file keep their place
seen.clear()
t0 = time.monotonic()
for text in ("hi", "are you there", "I have a question"):
    tc.add_message(user("dm_a", text))
    time.sleep(0.1)
tc.add_message(user("dm_b", "hello from b", sender="bob"))
tc.add_message(user("dm_a", "photo.jpg", ctype="attachment"))
tc.add_message(user("dm_a", "after the photo"))
wait_for(4)
check("three quick messages are one turn", seen and seen[0][1] == ["hi", "are you there", "I have a question"], seen[:1])
check("the turn waited for the chat to be quiet (stack_secs)", seen and seen[0][2] - t0 >= 0.6, seen[0][2] - t0 if seen else None)
check("another chat's message keeps its own turn", len(seen) > 1 and seen[1][1] == ["hello from b"], seen[1:2])
check("a file is its own turn and nothing after it jumps ahead of it",
      len(seen) > 3 and seen[2][1] == ["photo.jpg"] and seen[3][1] == ["after the photo"], seen[2:4])

# --- each new message restarts the wait
seen.clear()
t0 = time.monotonic()
for text in ("one", "two", "three", "four"):
    tc.add_message(user("dm_c", text))
    time.sleep(0.3)                      # below stack_secs: the chat never goes quiet
wait_for(1)
check("a message every 0.3 s (stack_secs 0.4) is still one turn", seen and seen[0][1] == ["one", "two", "three", "four"], seen)

# --- a message that arrives during a run waits for the next turn
seen.clear()
busy["secs"] = 1.0
tc.add_message(user("dm_d", "first"))
time.sleep(0.8)                          # the turn starts at ~0.4 s and runs 1 s
tc.add_message(user("dm_d", "during the run"))
wait_for(2)
busy["secs"] = 0
check("a message sent during a run is the next turn, not part of it",
      [s[1] for s in seen] == [["first"], ["during the run"]], [s[1] for s in seen])

# --- a profile's own stack_secs
profiles.remember("dm_slow", "slow", "dm")
seen.clear()
t0 = time.monotonic()
tc.add_message(user("dm_slow", "take your time", sender="patient"))
wait_for(1)
check("a profile's stack_secs (1.2) overrides the server's (0.4)", seen and seen[0][2] - t0 >= 1.1, seen[0][2] - t0 if seen else None)

# --- a timer fire, a person's messages and admin-turn wake-ups of one chat are one run
def fire(conv, skip_session=False):
    return {"role": "Proactive", "conversationId": conv, "senderInboxId": "ProactiveSystem",
            "source": "ProactiveSystem",
            "proactive_context": {"timer": {"timer_id": "t1", "skip_session": skip_session}}}


def wake(conv, text="Admin turn asked by the public profile of inbox x."):
    return {"role": "user", "source": "AdminTurn", "conversationId": conv, "kind": "dm",
            "contentType": "text", "adminProfile": "me", "timestamp": int(time.time() * 1000),
            "content": {"type": "text", "value": text}}


def while_busy(*messages):
    """Queue `messages` while the worker is held by another chat's run."""
    seen.clear()
    busy["secs"] = 1.0
    tc.add_message(user("dm_block", "hold the worker", sender="zed"))
    time.sleep(0.7)                      # its run started at ~0.4 s and lasts 1 s
    for m in messages:
        tc.add_message(m)
    busy["secs"] = 0
    time.sleep(0.9)
    wait_for(2)
    time.sleep(0.8)                      # let any further turn show up
    return [s[1] for s in seen[1:]]


got = while_busy(fire("dm_t"), user("dm_t", "hello"), wake("dm_t", "wake 1"))
check("a timer fire takes the chat's waiting message and wake-up into its run (live 2026-11-20: they ran apart)",
      got == [["<timer>", "hello", "wake 1"]], got)
got = while_busy(wake("dm_u", "wake 1"), fire("dm_u"), user("dm_u", "and me"))
check("a wake-up that comes first takes the waiting timer fire and what follows it",
      got == [["<timer>", "wake 1", "and me"]], got)
got = while_busy(fire("dm_v"))
check("a timer fire with nothing waiting runs alone", got == [["<timer>"]], got)
got = while_busy(fire("dm_w", skip_session=True), user("dm_w", "hello"))
check("a skip_session timer never shares its run", got == [["<timer>"], ["hello"]], got)
got = while_busy(fire("dm_x"), user("dm_x", "one"), fire("dm_x"), user("dm_x", "two"))
check("one fire per run: a second fire is the next run, with what came after it",
      got == [["<timer>", "one"], ["<timer>", "two"]], got)
grp = user("grp_y", "hi all")
grp["kind"] = "group"
got = while_busy(fire("grp_y"), grp)
check("a group stacks the same way: its timer fire takes the members' waiting messages",
      got == [["<timer>", "hi all"]], got)
got = while_busy(user("dm_z", "photo.jpg", ctype="attachment"), fire("dm_z"))
check("a file is still its own turn, the fire after it", got == [["photo.jpg"], ["<timer>"]], got)

# --- the batch reaches the model as one text; a group's lines carry names
cc = ChatController.__new__(ChatController)
text = cc._waiting_text([user("d", "are you there"), wake("d", "wake 1")], [user("d", "are you there")])
check("the fire's text lists what was waiting, in order", "are you there\nwake 1" in text, text)
check("and says a person is waiting for an answer", "A person wrote" in text, text)
check("wake-ups alone add no such line", "A person wrote" not in cc._waiting_text([wake("d")], []))
cc._resolve_sender_name = lambda inbox: ({"alice": "Alice"}.get(inbox), "test")
text = cc._waiting_text([user("g", "hi all", "alice"), wake("g", "wake 1")], [], True)
check("in a group each waiting line carries its sender",
      "Alice: hi all\nwake 1" in text and "A person wrote" not in text, text)
alone = []
cc.thread_controller = tc
cc.receive_Message_ChatController = lambda m: alone.append(m) or "handled"
out = cc._answer_alone([user("dm_r", "a"), user("dm_r", "b")])
check("messages stacked on a refused timer still get their turn, together",
      out == "handled" and len(alone) == 1 and len(alone[0].get("batch") or []) == 2, alone)
check("nothing stacked on a refused timer: nothing runs", cc._answer_alone([]) is None)
cc._resolve_sender_name = lambda inbox: ({"alice": "Alice", "bob": "Bob"}.get(inbox), "test")
msg = {"content": {"type": "text", "value": "x"}}
cc._merge_batch(msg, [user("g", "hi all", "alice"), user("g", "hey", "bob")], True)
check("a group batch is one text, each line with its sender", msg["content"]["value"] == "Alice: hi all\nBob: hey", msg)
check("and is not prefixed again", msg.get("prefixed") is True)
msg = {"content": {"type": "text", "value": "x"}}
cc._merge_batch(msg, [user("d", "a"), user("d", "b")], False)
check("a DM batch is the messages in order", msg["content"]["value"] == "a\nb", msg)

# --- group pacing and [NO_REPLY]
cc.thread_controller = tc
check("[NO_REPLY] in a group sends nothing", cc._group_may_send("group_free", "[NO_REPLY]", "t") is False)
check("a note then [NO_REPLY] on its own line sends nothing (2026-11-18 leak)",
      cc._is_no_reply("Nothing to report this turn.\n\n[NO_REPLY]") is True)
check("the trailing tag with markdown around it still counts",
      cc._is_no_reply({"type": "text", "value": "done.\n**[NO_REPLY]**"}) is True)
check("the tag mid-sentence does not silence a real answer",
      cc._is_no_reply("I answer [NO_REPLY] when there is nothing to say.") is False)
check("a normal answer is not a no-reply", cc._is_no_reply("hello") is False)
check("a group answer goes out with no limit set", cc._group_may_send("group_free", "hello", "t") is True)
check("and again (group_reply_secs = 0)", cc._group_may_send("group_free", "hello again", "t") is True)
profiles.remember("group_chatty", "chatty", "group")
check("a bot with group_reply_secs posts once", cc._group_may_send("group_chatty", "my take", "t") is True)
check("then is held back", cc._group_may_send("group_chatty", "one more thing", "t") is False)
check("the turn text tells it it may not post, and until when",
      "may not post here until" in group_pace.note("group_chatty"), group_pace.note("group_chatty"))
check("a free group's turn text says it may post", "You may post here" in group_pace.note("group_free"))
group_pace._posts = None                 # a restart
check("the last post time survives a restart", group_pace.allowed("group_chatty") is False)

print(f"\n{'ALL OK' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
