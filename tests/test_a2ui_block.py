"""<a2ui> (modules/a2ui_block.py): the block, its check, the fixes a turn gets, what
comes back from the app, and the round trip through the thread controller and the
chat controller. The model is a script. No Claude, no XMTP.

Needs the A2UI agent SDK (pip install a2ui-agent-sdk); without it the test says so
and stops with exit code 1.

    python tests/test_a2ui_block.py

Exit code 1 if any check fails.
"""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)[:600]) if detail and not ok else ""))
    if not ok:
        failures.append(name)


DATA = tempfile.mkdtemp(prefix="gpette_a2ui_")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write(f'''[server]
data_dir = "{DATA.replace(chr(92), "/")}"
stack_secs = 0.3

[daemon]
url = "http://127.0.0.1:9"

[llm]
provider = "claude-code"

[senders]
resolver = ""

[gate]
kind = "none"

[profiles.main]
members = ["owner"]
gate = "open"
a2ui = true

[profiles.plain]
members = ["guest"]
gate = "open"

[profiles.club]
members = ["group_club"]
gate = "open"
a2ui = true
''')
os.environ["GPETTE_CONFIG"] = CONFIG

from config import Config  # noqa: E402
from modules import a2ui_block, profiles, prompts  # noqa: E402
from modules.chat_controller import ChatController  # noqa: E402
from modules.function_registry import FunctionRegistry  # noqa: E402
from modules.thread_controller import ThreadController  # noqa: E402

ok, why = a2ui_block.available()
if not ok:
    print(f"FAIL the A2UI agent SDK did not load: {why}\n     pip install a2ui-agent-sdk")
    sys.exit(1)

MENU = '''<a2ui>
surface("menu")
$/title = "Assistant menu"
$/items = [{id: "status", label: "Status"}, {id: "model", label: "Model"}]
btn = Button(Text($label), action=Event("menu_press", {id: $id}))
root = Card(Column([Text("Pick one"), Text($/title), Column(_template($/items, btn))]))
</a2ui>'''
UPDATE = '''<a2ui>
surface("menu")
$/title = "Pick a model"
$/items = [{id: "opus", label: "Opus"}, {id: "back", label: "Back"}]
</a2ui>'''
BAD = '''<a2ui>
surface("x")
root = Card(Column([Text("hi"), WalletSigner("1000")]))
</a2ui>'''
# What the Flutter renderer (a2ui_flutter 0.4.0) sends for a press on "Model".
PRESS = {"messages": [{"version": "1.0", "action": {
    "name": "menu_press", "surfaceId": "menu", "sourceComponentId": "btn",
    "timestamp": "2026-11-23T07:05:39.347799Z", "context": {"id": "model"}}}]}


def kinds(parts):
    return ["a2ui" if "a2ui" in p else "text" for p in parts]


def keys(part):
    return [next(iter(k for k in m if k != "version")) for m in part["a2ui"]["messages"]]


# --- reading an answer ---------------------------------------------------------------------
check("an answer with no block is left alone", not a2ui_block.has_block("just an answer"))
check("strip() leaves the text around the block",
      a2ui_block.strip("Here.\n" + MENU + "\nPick one.") == "Here.\n\nPick one.",
      a2ui_block.strip("Here.\n" + MENU + "\nPick one."))
check("strip() cuts a block that was never closed", a2ui_block.strip("Hi.\n<a2ui>\nroot = Text(") == "Hi.")

# --- the check -----------------------------------------------------------------------------
outcome, parts = a2ui_block.run("Here is the menu.\n" + MENU + "\nPick one.", "c1")
check("a good block is to be sent, text and surface in the order written",
      outcome == "send" and kinds(parts) == ["text", "a2ui", "text"], (outcome, parts))
check("the block became one createSurface for surface \"menu\"",
      keys(parts[1]) == ["createSurface"]
      and parts[1]["a2ui"]["messages"][0]["createSurface"]["surfaceId"] == "menu", parts[1])
check("the payload is plain JSON", json.loads(json.dumps(parts[1]["a2ui"])) == parts[1]["a2ui"])
check("the fallback line holds the surface's own words", "Pick one" in parts[1]["fallback"], parts[1]["fallback"])
check("the history line names the surface", "surface menu" in a2ui_block.stored_text(parts[1]),
      a2ui_block.stored_text(parts[1]))

outcome, parts = a2ui_block.run("Sure.\n" + UPDATE, "c1")
check("a later block for the same surface is data patches, no new surface",
      outcome == "send" and keys(parts[1]) == ["updateDataModel", "updateDataModel"], (outcome, parts))
outcome, result = a2ui_block.run(UPDATE, "c_other")
check("the same update in a chat that never got the surface is refused",
      outcome == "retry" and "Surface not found" in result, (outcome, result))

a2ui_block.start_turn("c2")
outcome, result = a2ui_block.run(BAD, "c2")
check("a component that is not in the catalog is refused, with the reason",
      outcome == "retry" and result.startswith(a2ui_block.RESULT) and "WalletSigner" in result, (outcome, result))
check("the result says how many attempts are left", "2 more attempts" in result, result)
outcome, result = a2ui_block.run(BAD, "c2")
check("the second failure leaves one", outcome == "retry" and "1 more attempt " in result, result)
outcome, result = a2ui_block.run("Sorry.\n" + BAD, "c2")
check("the third gives up: the answer without its block", (outcome, result) == ("plain", "Sorry."), (outcome, result))
a2ui_block.start_turn("c2")
outcome, result = a2ui_block.run(BAD, "c2")
check("a new turn starts the count over", outcome == "retry" and "2 more attempts" in result, result)
outcome, parts = a2ui_block.run(MENU, "c2")
check("a refused block left nothing behind: the surface can still be created",
      outcome == "send" and keys(parts[0]) == ["createSurface"], (outcome, parts))
a2ui_block.forget("c1")
outcome, result = a2ui_block.run(UPDATE, "c1")
check("after forget() (a reset chat) the old surface is gone", outcome == "retry", (outcome, result))

# --- what comes back -----------------------------------------------------------------------
text = a2ui_block.incoming_text(PRESS)
check("a button press reads as text for the model",
      text.startswith(a2ui_block.ACTION) and '"menu_press"' in text and '"menu"' in text
      and '{"id": "model"}' in text, text)
text = a2ui_block.incoming_text({"messages": [{"version": "1.0", "error": {
    "code": "UNKNOWN_COMPONENT_TYPE", "surfaceId": "menu", "message": "no such component"}}]})
check("an error from the app reads as one line", "UNKNOWN_COMPONENT_TYPE" in text and "menu" in text, text)
check("a payload with nothing in it is empty text", a2ui_block.incoming_text({"messages": [{"x": 1}]}) == "")
check("so is a missing payload", a2ui_block.incoming_text(None) == "")

# --- the lesson ----------------------------------------------------------------------------
path = a2ui_block.write_skill()
lesson = open(path, encoding="utf-8").read()
check("the lesson file is written under the data folder", path.startswith(DATA) and len(lesson) > 5000, path)
check("it teaches the tags and the catalog", "<a2ui>" in lesson and "Button(" in lesson and "ChoicePicker(" in lesson)
main = profiles.resolve_profile("dm_owner", "owner", "dm")
plain = profiles.resolve_profile("dm_guest", "guest", "dm")
check("a profile with a2ui = true is told about the block and where the lesson is",
      "<a2ui>" in prompts.blocks_note(main) and path in prompts.blocks_note(main))
check("a profile without it is not", "<a2ui>" not in prompts.blocks_note(plain))

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


def user(conv, text, sender, kind="dm"):
    return {"role": "user", "conversationId": conv, "senderInboxId": sender, "source": "SocketIO",
            "contentType": "text", "kind": kind, "timestamp": int(time.time() * 1000),
            "content": {"type": "text", "value": text}}


def turn(conv, sender, text, answers, wait_heard, wait_out=1, kind="dm"):
    del heard[:], out[:]
    script[:] = answers
    tc.add_message(user(conv, text, sender, kind))
    end = time.time() + 15
    while (len(heard) < wait_heard or len([m for m in out if m.get("role") == "assistant"]) < wait_out) \
            and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.3)
    return [m for m in out if m.get("role") == "assistant"]


def shape(sent):
    return [(m.get("contentType") or "text") for m in sent]


# 1. an answer with a block: text, surface, text
sent = turn("dm_owner", "owner", "menu please", ["Here it is.\n" + MENU + "\nPick one."], 1, wait_out=3)
check("the chat gets text, the surface, text, in that order", shape(sent) == ["text", "a2ui", "text"], shape(sent))
check("the surface message carries the A2UI payload and a fallback",
      sent[1]["a2ui"]["messages"][0]["createSurface"]["surfaceId"] == "menu" and sent[1]["fallback"], sent[1])
check("the block itself never reaches the chat as text",
      not any("<a2ui>" in (m["content"]["value"] or "") for m in sent), sent)
stored = [(m.get("content") or {}).get("value") for m in tc.get_chat_history("dm_owner", limit=6)]
check("the history keeps a line for the surface", any("surface menu" in (s or "") for s in stored), stored)

# 2. the press comes back as a message, and the answer updates the surface in place
sent = turn("dm_owner", "owner", a2ui_block.incoming_text(PRESS), [UPDATE], 1)
check("the model heard the press", heard and heard[0]["text"].startswith(a2ui_block.ACTION), heard)
check("its answer went out as data patches for the same surface",
      shape(sent) == ["a2ui"] and keys(sent[0]) == ["updateDataModel", "updateDataModel"], sent)

# 3. a refused block goes back to the model, which fixes it
sent = turn("dm_owner", "owner", "something new", [BAD, "Fixed.\n" + MENU.replace('"menu"', '"menu2"')], 2, wait_out=2)
tools = [h["text"] for h in heard if h["role"] == "tool"]
check("the model got the reason", tools and "WalletSigner" in tools[0], heard)
check("nothing of the refused answer reached the chat; the fixed one did",
      shape(sent) == ["text", "a2ui"] and sent[0]["content"]["value"] == "Fixed.", sent)

# 4. three refusals: the last answer goes out without its block
sent = turn("dm_owner", "owner", "again", [BAD, BAD, "I could not draw it.\n" + BAD], 3)
check("after the fixes are used up the person gets the words only",
      shape(sent) == ["text"] and sent[0]["content"]["value"] == "I could not draw it.", sent)

# 5. a profile without the switch, and a group
sent = turn("dm_guest", "guest", "menu please", ["Here.\n" + MENU], 1)
check("without a2ui = true the block is cut and the text goes out",
      shape(sent) == ["text"] and sent[0]["content"]["value"] == "Here.", sent)
sent = turn("group_club", "owner", "menu please", ["Here.\n" + MENU], 1, kind="group")
check("in a group the block is cut too", shape(sent) == ["text"] and sent[0]["content"]["value"] == "Here.", sent)

# --- the way out -----------------------------------------------------------------------------
from server import server as srv  # noqa: E402

emitted = []
srv._emit_outgoing = emitted.append
srv.sendMessage({"conversationId": "dm_owner", "contentType": "a2ui", "a2ui": PRESS, "fallback": "A menu",
                 "content": {"type": "text", "value": "kept for the history"}})
check("sendMessage hands the daemon the payload, not the history line",
      emitted == [{"conversationId": "dm_owner", "contentType": "a2ui", "a2ui": PRESS, "fallback": "A menu"}], emitted)
std = srv.standardize_message({"conversationId": "dm_owner", "senderInboxId": "owner", "contentType": "a2ui",
                               "kind": "dm", "content": "", "a2ui": PRESS})
check("an incoming envelope keeps its a2ui payload", std.get("a2ui") == PRESS, std)

print(f"\n{'ALL OK' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
