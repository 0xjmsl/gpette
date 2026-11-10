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

# 2. Claude Code: untouched, no call, nothing saved.
cc = ClaudeCodeService.__new__(ClaudeCodeService)
for i in range(1, 41):
    db.store_message(msg("c2", i))
h = history("c2")
out, note = compact(h, cc, "c2", db)
check("Claude Code: history untouched", out == h and note is None)
check("Claude Code: nothing saved", db.get_summary("c2") is None)

# 3. disabled
Config.COMPACTION_PROVIDER = "disabled"
model3 = FakeModel()
out, note = compact(h, model3, "c2", db)
check("disabled: untouched, no call", out == h and note is None and not model3.calls)
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
