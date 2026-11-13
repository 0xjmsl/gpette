"""Running-summary compaction, checked without a model or XMTP.

    python tests/test_compaction.py

Uses a temporary SQLite store and a fake API model that records every fold call.
Exit code 1 if any check fails.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("GPETTE_CONFIG", os.path.join(tempfile.mkdtemp(), "none.toml"))

from config import Config  # noqa: E402

TMP = tempfile.mkdtemp(prefix="gpette_compaction_")
Config.DATA_DIR = TMP
Config.STORAGE_PATH = os.path.join(TMP, "test.db")
Config.COMPACTION_PROVIDER = "same"
Config.COMPACTION_AFTER = 30
Config.COMPACTION_KEEP = 10
Config.THREAD_JOINS_ENABLED = True
Config.THREAD_JOINS = [["joinA", "joinB"]]

from models.conversation_db import ConversationDB  # noqa: E402
from models.claudeCodeService import ClaudeCodeService  # noqa: E402
from modules import context_compactor  # noqa: E402
from modules.context_compactor import compact  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class FakeModel:
    """An API model: answers quick_call with a summary that names every message it saw."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def quick_call(self, prompt, max_tokens, model=None):
        self.calls.append(prompt)
        if self.fail:
            return None
        seen = [line.split(": ", 1)[1] for line in prompt.split("Newer messages:")[1].strip().splitlines()]
        return f"summary #{len(self.calls)} covering {seen[0]}..{seen[-1]}"


db = ConversationDB()
T0 = 1_700_000_000_000


def msg(conv, i, role=None, **extra):
    m = {"conversationId": conv, "senderInboxId": "u1", "role": role or ("user" if i % 2 else "assistant"),
         "timestamp": T0 + i * 1000, "content": {"type": "text", "value": f"m{i}"}, "source": "test"}
    m.update(extra)
    return m


def history(conv, limit=50):
    return db.get_conversation_history(context_compactor.summary_key(conv).split("|"), limit=limit)


# 1. A long chat, message by message: one fold per 21 messages, never one per message.
model = FakeModel()
lens, notes = [], []
for i in range(1, 61):
    db.store_message(msg("c1", i))
    out, note = compact(history("c1"), model, "c1", db)
    lens.append(len(out))
    notes.append(note)
check("no fold below the limit", len(model.calls) >= 1 and lens[29] == 30 and notes[29] is None)
check("first fold at message 31, keeps 10", lens[30] == 10 and notes[30] and "summary #1" in notes[30],
      f"len={lens[30]} note={notes[30]}")
check("two folds over 60 messages", len(model.calls) == 2, f"calls={len(model.calls)}")
check("never more than 30 messages sent", max(lens) <= 30, f"max={max(lens)}")
saved = db.get_summary("c1")
check("summary saved with its marker", saved and saved["through_ts"] == T0 + 42 * 1000 and "summary #2" in saved["summary"],
      f"saved={saved}")
check("second fold carries the first summary", "summary #1 covering m1..m21" in model.calls[1])
check("second fold sees only new messages", "m21" not in model.calls[1].split("Newer messages:")[1]
      and "m22" in model.calls[1])
out, note = compact(history("c1"), model, "c1", db)
check("after the fold: the 18 newer messages, word for word", [m["content"]["value"] for m in out][:1] == ["m43"]
      and len(out) == 18, f"{[m['content']['value'] for m in out][:3]} len={len(out)}")

# 2. Claude Code: the trigger is the size of the session's context.
class FakeClaudeCode(ClaudeCodeService):
    """Claude Code without the CLI: a session of a given size, and one-shot calls
    that are recorded."""

    def __init__(self, context=None, topic="SAME", fail=False):
        self.context, self.topic, self.fail = context, topic, fail
        self.calls, self.cleared = [], 0

    def context_size(self, conversation_id, profile=None):
        return self.context

    def clear_session(self, conversation_id, profile=None):
        self.cleared += 1
        self.context = None

    def quick_call(self, prompt, max_tokens, model=None):
        self.calls.append((prompt, model))
        if "SAME or NEW" in prompt:
            return self.topic
        if self.fail:
            return None
        seen = [line.split(": ", 1)[1] for line in prompt.split("Newer messages:")[1].strip().splitlines()]
        return f"cc summary covering {seen[0]}..{seen[-1]}"


def values(messages):
    return [m["content"]["value"] if isinstance(m["content"], dict) else m["content"] for m in messages]


def ms(a, b):
    return [f"m{i}" for i in range(a, b + 1)]


def turn(conv, i, cc, text=None, **kw):
    """A user message arrives (stored first, as the server does), then the compactor runs."""
    incoming = msg(conv, i, "user")
    if text:
        incoming["content"] = {"type": "text", "value": text}
    db.store_message(incoming)
    passed = history(conv)[:-1]
    out, note = compact(passed, cc, conv, db, incoming=incoming, **kw)
    return out, note, incoming, passed


SUMMARY = "[Summary of the earlier conversation: {}]"
for i in range(1, 41):
    db.store_message(msg("cc1", i))

cc = FakeClaudeCode(context=None)
out, note, _, _ = turn("cc1", 41, cc)
check("Claude Code, no session: starts with the latest 10 messages, no call",
      values(out) == ms(31, 40) and note is None and not cc.calls, f"{values(out)} {note}")
check("Claude Code, no session: nothing saved", db.get_summary("cc1") is None)

cc = FakeClaudeCode(context=100_000)
out, note, _, passed = turn("cc1", 42, cc)
check("small session: no call, no cut", not cc.calls and cc.cleared == 0 and out == passed and note is None)

cc = FakeClaudeCode(context=350_000, topic="SAME")
out, note, _, _ = turn("cc1", 43, cc)
check("topic band, same topic: one short call on sonnet, no cut",
      len(cc.calls) == 1 and cc.calls[0][1] == "sonnet" and cc.cleared == 0 and db.get_summary("cc1") is None,
      f"calls={len(cc.calls)}")
check("the topic call sees the latest messages and the new one",
      "user: m42" in cc.calls[0][0] and cc.calls[0][0].rstrip().endswith("m43"))

cc = FakeClaudeCode(context=350_000, topic="NEW.")
out, note, _, _ = turn("cc1", 44, cc)
saved = db.get_summary("cc1")
check("topic band, new topic: the log is folded and the session dropped",
      len(cc.calls) == 2 and cc.cleared == 1 and saved and saved["summary"] == "cc summary covering m1..m33"
      and saved["through_ts"] == T0 + 33 * 1000, f"calls={len(cc.calls)} saved={saved}")
check("the fold asks for up to 1000 words", "at most 1000 words" in cc.calls[1][0])
check("the new session starts with the summary and the latest 10 messages",
      values(out) == ms(34, 43) and note == SUMMARY.format("cc summary covering m1..m33"), f"{values(out)} {note}")

cc = FakeClaudeCode(context=450_000, fail=True)
out, note, _, _ = turn("cc1", 45, cc)
check("forced, summary call fails: no cut, nothing changed",
      len(cc.calls) == 1 and cc.cleared == 0 and db.get_summary("cc1")["summary"] == "cc summary covering m1..m33"
      and values(out) == ms(34, 44), f"{values(out)}")
check("and the status says so", context_compactor.get_status()["outcome"] == "fold_failed"
      and context_compactor.get_status()["report"].startswith("Not compacted:"))

cc = FakeClaudeCode(context=450_000)
out, note, _, _ = turn("cc1", 46, cc)
check("forced: cut without a topic call", len(cc.calls) == 1 and "SAME or NEW" not in cc.calls[0][0] and cc.cleared == 1)
check("the fold carries the earlier summary and only the newer messages",
      "cc summary covering m1..m33" in cc.calls[0][0].split("Newer messages:")[0]
      and [line.split(": ", 1)[1] for line in cc.calls[0][0].split("Newer messages:")[1].strip().splitlines()]
      == ["m34", "m35"], cc.calls[0][0][-200:])
check("forced: the latest 10 start the new session", values(out) == ms(36, 45)
      and note == SUMMARY.format("cc summary covering m34..m35"), f"{values(out)} {note}")

cc = FakeClaudeCode(context=450_000)
out, note, _, passed = turn("cc1", 47, cc, fold=False)
check("fold=False: no call, no cut", not cc.calls and cc.cleared == 0)

# /compact cuts at any size and leaves its report on the message.
cc = FakeClaudeCode(context=5_000)
out, note, incoming, _ = turn("cc1", 48, cc, text="/compact")
report = incoming.get("compactReport") or ""
check("/compact cuts a small session", cc.cleared == 1 and report.startswith("Compacted (asked, 5,000 tokens)"), report)
db.store_message({**msg("cc1", 49, "assistant"), "content": {"type": "text", "value": report}})
cc = FakeClaudeCode(context=None)
out, note, _, _ = turn("cc1", 50, cc)
check("the command and its answer are not conversation", values(out) == ms(38, 47), f"{values(out)}")

# A timer fire is folded as one line and never opens a new session's context.
for i in range(1, 21):
    if i == 6:
        db.store_message({**msg("cc2", i, "assistant"), "content": {"type": "text", "value": ""},
                          "tool_calls": [{"id": "p1"}], "source": "ProactiveSystem"})
        db.store_message({**msg("cc2", i, "tool"), "content": "Timer fired: check the mail",
                          "tool_call_id": "p1", "timestamp": T0 + i * 1000 + 1, "source": "ProactiveSystem"})
    else:
        db.store_message(msg("cc2", i))
cc = FakeClaudeCode(context=450_000)
out, note, _, _ = turn("cc2", 21, cc)
folded = cc.calls[0][0].split("Newer messages:")[1]
check("a timer fire is folded as one line", "timer: Timer fired: check the mail" in folded and "m10" in folded
      and "m11" not in folded, folded)
check("and is never part of the latest messages", values(out) == ms(11, 20), f"{values(out)}")

# A reset starts clean: the summary is emptied and nothing before it is carried over.
cc = FakeClaudeCode(context=50_000)
out, note, _, _ = turn("cc1", 51, cc, text="/reset")
saved = db.get_summary("cc1")
check("reset: summary emptied, marker = now", out == [] and note is None and saved["summary"] == ""
      and saved["through_ts"] > T0 + 10_000_000, f"{saved}")
out, note, _, _ = turn("cc1", 52, FakeClaudeCode(context=None))
check("after a reset the next session starts with nothing", out == [] and note is None, f"{values(out)} {note}")

# The service side: what a new session is opened with, and the commands' answers.
svc = ClaudeCodeService.__new__(ClaudeCodeService)
svc._sessions, svc._empty_counters = {}, {}
svc._save_sessions = lambda: None
opening = svc._build_context([msg("e", i) for i in range(1, 16)], SUMMARY.format("S"))
check("a new session opens with the summary, then the latest 10 messages",
      opening.startswith(SUMMARY.format("S") + "\n\n[Recent conversation context:]\n[assistant]: m6\n")
      and "m5" not in opening and opening.endswith("[user]: m15\n\n---\n\n"), opening)
check("nothing to open with: nothing is added", svc._build_context([], "") == "")
sent = []


def fake_run(text, conversation_id, session_id=None, profile=None, note=""):
    sent.append((text, session_id))
    return {"result": "hi", "session_id": "S1", "total_cost_usd": 0.1, "num_turns": 4,
            "usage": {"cache_read_input_tokens": 1_200_000, "output_tokens": 900, "iterations": [
                {"input_tokens": 10, "cache_read_input_tokens": 320_000,
                 "cache_creation_input_tokens": 5, "output_tokens": 85}]}}


svc._run_claude = fake_run
first = msg("e2e", 1, "user")
first["contextNote"] = SUMMARY.format("S")
svc.send_message(first, [msg("e2e", 0, "assistant")], "")
check("send_message: a new session gets summary + latest messages + the message",
      sent and sent[0] == (SUMMARY.format("S") + "\n\n[Recent conversation context:]\n[assistant]: m0\n\n---\n\nm1", None),
      f"{sent}")
check("the session's size is the last model call of the run", svc.context_size("e2e") == 320_100,
      f"{svc.context_size('e2e')}")
check("no session on file: size is None", svc.context_size("nobody") is None)
second = msg("e2e", 2, "user")
second["contextNote"] = SUMMARY.format("S")
svc.send_message(second, [msg("e2e", 0, "assistant")], "")
check("a resumed session gets the message alone", sent[1] == ("m2", "S1"), f"{sent[1]}")
ask = {**msg("e2e", 3, "user"), "content": {"type": "text", "value": "/compact"}, "compactReport": "Compacted (asked"}
reply = svc.send_message(ask, [], "")
check("/compact is answered with the report, without a model turn",
      reply["content"]["value"] == "Compacted (asked" and len(sent) == 2)
ask.pop("compactReport")
check("/compact with compaction off says so",
      svc.send_message(ask, [], "")["content"]["value"] == context_compactor.COMPACT_OFF_REPLY)
reply = svc.send_message({**msg("e2e", 4, "user"), "content": {"type": "text", "value": "New Session"}}, [], "")
check("a reset phrase drops the session", reply["content"]["value"] == context_compactor.RESET_REPLY
      and svc.context_size("e2e") is None and len(sent) == 2)

# The API checks below need a history no summary was saved for.
for i in range(1, 41):
    db.store_message(msg("c2", i))
h = history("c2")

# 3. disabled
Config.COMPACTION_PROVIDER = "disabled"
model3 = FakeModel()
out, note = compact(h, model3, "c2", db)
check("disabled: untouched, no call", out == h and note is None and not model3.calls)
cc3 = FakeClaudeCode(context=450_000)
out, note = compact(h, cc3, "c2", db, incoming=h[-1])
check("disabled: Claude Code is left alone too", out == h and note is None and not cc3.calls and cc3.cleared == 0)
Config.COMPACTION_PROVIDER = "same"

# 4. A failed fold saves nothing and is retried on the next message.
bad = FakeModel(fail=True)
out, note = compact(h, bad, "c2", db)
check("failed fold: full pending history, nothing saved", len(out) == 40 and db.get_summary("c2") is None)
good = FakeModel()
out, note = compact(h, good, "c2", db)
check("retry folds", len(good.calls) == 1 and db.get_summary("c2") is not None and len(out) == 10)

# 5. A kept tool result never loses its call.
for i in range(1, 32):
    role = None
    extra = {}
    if i == 21:
        role, extra = "assistant", {"tool_calls": [{"id": "t1"}], "content": None}
    if i == 22:
        role, extra = "tool", {"tool_call_id": "t1"}
    db.store_message(msg("c3", i, role, **extra))
out, note = compact(history("c3"), FakeModel(), "c3", db)
check("tool result folded together with its call", out[0]["role"] != "tool" and len(out) == 9,
      f"first={out[0]['role']} len={len(out)}")

# 6. fold=False (mid tool loop): the summary applies, no model call.
for i in range(32, 60):
    db.store_message(msg("c3", i))
m6 = FakeModel()
out, note = compact(history("c3"), m6, "c3", db, fold=False)
check("mid tool loop: no call, summary still applied", not m6.calls and note is not None and len(out) > 30)

# 7. A reset clears the summary.
db.reset_conversation("c3")
check("reset clears the summary", db.get_summary("c3") is None)

# 8. Joined threads share one summary.
for i in range(1, 32):
    db.store_message(msg("joinA" if i % 2 else "joinB", i))
compact(history("joinA"), FakeModel(), "joinA", db)
check("joined threads share one summary", db.get_summary("joinA|joinB") is not None
      and db.get_summary("joinA") is None)
db.reset_conversation("joinB")
check("resetting one joined thread clears the shared summary", db.get_summary("joinA|joinB") is None)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
