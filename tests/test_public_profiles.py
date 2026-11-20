"""Public profiles: the inbox list (people a bot wrote to first), one folder per
person (`per_inbox`), and the rule that gpette only ever puts someone in a profile
that holds no power. No model, no XMTP.

    python tests/test_public_profiles.py

Exit code 1 if any check fails.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DATA = tempfile.mkdtemp(prefix="gpette_public_")
ROOT = os.path.join(DATA, "public").replace(chr(92), "/")
CONFIG = os.path.join(DATA, "gpette.toml")
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write(f'''[server]
data_dir = "{DATA.replace(chr(92), "/")}"

[daemon]
nothing_matched = "bot_public"

[gate]
kind = "password"
password = "open sesame"

[profiles.bot_main]
members = ["{'0' * 64}"]
gate = "open"
workdir = "{DATA.replace(chr(92), "/")}/main"
allowed_tools = ["*"]
admin = true
menu = true
send = true
public_profile = "bot_public"

[profiles.bot_public]
gate = "password"
workdir = "{ROOT}"
per_inbox = true
allowed_tools = ["Read", "Glob", "Grep"]
knowledge = "{DATA.replace(chr(92), "/")}/knowledge"
admin_profile = "bot_main"
''')
os.environ["GPETTE_CONFIG"] = CONFIG

from config import Config  # noqa: E402
from modules import profiles  # noqa: E402
from modules.gates import get_gate  # noqa: E402
from models.conversation_db import ConversationDB  # noqa: E402

failures = []
OWNER, X, Y, Z = "0" * 64, "a" * 64, "b" * 64, "c" * 64


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def msg(text, sender):
    return {"conversationId": "dm_" + sender[:4], "senderInboxId": sender, "contentType": "text",
            "content": {"type": "text", "value": text}}


db = ConversationDB()
profiles.check_config()
check("the config passes the boot check", True)
main = profiles.resolve_profile("dm_owner", OWNER, "dm")

# --- a stranger: the public profile, behind its password ----------------------------
p = profiles.resolve_profile("dm_y", Y, "dm")
check("a stranger meets the public profile (nothing_matched)", p.name == "bot_public" and p.gate == "password", (p.name, p.gate))
check("a stranger without the password is dropped",
      get_gate(p.gate, db).check_message(msg("hello", Y), kind="dm")["action"] == "reject")
check("the password lets them in",
      get_gate(p.gate, db).check_message(msg("open sesame", Y), kind="dm")["action"] == "auth_success")
p = profiles.resolve_profile("dm_y", Y, "dm")
check("and then they are answered, still in the public profile",
      p.name == "bot_public" and get_gate(p.gate, db).check_message(msg("hi again", Y), kind="dm")["action"] == "allow")
folder_y = profiles.workdir_for(p, "dm_y")
check("in a folder of their own, named by their inbox", folder_y.replace(chr(92), "/") == f"{ROOT}/{Y}", folder_y)

# --- someone the bot wrote to first ------------------------------------------------------
line = profiles.register_outreach(main, X)
check("writing first makes their folder and says so", line == f"New public folder created for inbox {X}.", line)
check("the folder exists", os.path.isdir(os.path.join(ROOT, X)))
check("writing to them again says nothing new", profiles.register_outreach(main, X) == "")
row = db.registered_inbox(X)
check("the list remembers who added them and why",
      row and row["profile"] == "bot_public" and row["reason"] == "outreach" and row["added_by"] == "bot_main", row)
p = profiles.resolve_profile("dm_x", X, "dm")
check("their answer gets the public profile with no password", p.name == "bot_public" and p.gate == "open" and p.via_inbox_list,
      (p.name, p.gate))
check("and its own folder, not Y's", profiles.workdir_for(p, "dm_x").replace(chr(92), "/") == f"{ROOT}/{X}")
check("a tool result in that chat runs in the same folder",
      profiles.workdir_for(profiles.profile_for_conversation("dm_x", "dm"), "dm_x").replace(chr(92), "/") == f"{ROOT}/{X}")
check("the public profile has none of main's power",
      not p.admin and not p.send and not p.menu and not p.inherit_user_settings and p.allowed_tools == ["Read", "Glob", "Grep"],
      p.allowed_tools)
check("it reads the shared knowledge folder too", p.knowledge.replace(chr(92), "/").endswith("/knowledge"), p.knowledge)
check("its admin turns go to main", profiles.admin_target(p) == ("bot_main", None))

# --- the list never moves anyone out of a profile -----------------------------------------
check("writing to someone already in a config profile adds nothing", profiles.register_outreach(main, OWNER) == "")
check("the owner is not on the list", db.registered_inbox(OWNER) is None)
p = profiles.resolve_profile("dm_owner", OWNER, "dm")
check("the owner keeps the main profile", p.name == "bot_main" and not p.via_inbox_list, p.name)
db.register_inbox(OWNER, "bot_public", "forged")          # a row written straight into the database
p = profiles.resolve_profile("dm_owner", OWNER, "dm")
check("even with a row for them, a config member stays in their profile", p.name == "bot_main", p.name)

# --- the list can only ever give a powerless profile ----------------------------------------
db.register_inbox(Z, "bot_main", "forged")                # someone writes main's name into the table
p = profiles.resolve_profile("dm_z", Z, "dm")
check("a row that names a profile with power is ignored", p.name == "bot_public" and p.gate == "password", (p.name, p.gate))
check("public_ok says why", "per_inbox" in (profiles.public_ok("bot_main") or ""), profiles.public_ok("bot_main"))
for key, value, word in (("admin", True, "admin"), ("send", True, "send"), ("menu", True, "menu"),
                         ("inherit_user_settings", True, "inherit"), ("allowed_tools", ["*"], "every tool"),
                         ("allowed_tools", ["Read", "Bash"], "Bash"),
                         ("allowed_tools", ["Read", "Bash(ls *)"], "Bash"),
                         ("per_inbox", False, "per_inbox")):
    saved = Config.PROFILES["bot_public"].get(key)
    Config.PROFILES["bot_public"][key] = value
    why = profiles.public_ok("bot_public") or ""
    check(f"a public profile with {key} = {value} is refused", word in why, why)
    try:
        profiles.check_config()
        check(f"  and gpette does not start with it", False)
    except ValueError as e:
        check(f"  and gpette does not start with it", "public_profile" in str(e), e)
    if saved is None:
        del Config.PROFILES["bot_public"][key]
    else:
        Config.PROFILES["bot_public"][key] = saved
check("the config is whole again", profiles.public_ok("bot_public") is None, profiles.public_ok("bot_public"))

# --- the fence of a per_inbox run: file tools stay in the person's own folder -----------------
from models.claudeCodeService import ClaudeCodeService as CC  # noqa: E402
rules = CC._per_inbox_tools(["Read", "Glob", "Grep", "Write", "Edit"], "")
check("every unscoped file tool is limited to the run's own folder",
      rules == ["Read(./**)", "Glob(./**)", "Grep(./**)", "Write(./**)", "Edit(./**)"], rules)
check("a scope the config wrote is kept", CC._per_inbox_tools(["Read(./pics/**)"], "") == ["Read(./pics/**)"])
rules = CC._per_inbox_tools(["Read", "Write"], os.path.join(DATA, "knowledge"))
k = CC._abs_rule(os.path.join(DATA, "knowledge"))
check("the knowledge folder is added for the read tools only",
      f"Read({k}/**)" in rules and not any(r.startswith("Write(//") for r in rules), rules)
deny = CC._knowledge_denials(os.path.join(DATA, "knowledge"))
check("and writing there is denied outright", f"Write({k}/**)" in deny and f"Edit({k}/**)" in deny, deny)
check("an absolute rule starts with // and a drive letter is lowered",
      k.startswith("//") and ":" not in k, k)

# while running: the public profile loses its fence -> the list stops giving it
Config.PROFILES["bot_public"]["send"] = True
p = profiles.resolve_profile("dm_x", X, "dm")
check("a public profile changed under a running gpette is no longer handed out by the list",
      not p.via_inbox_list, (p.name, p.via_inbox_list))
check("and a new first message registers nobody", profiles.register_outreach(main, "d" * 64) == ""
      and db.registered_inbox("d" * 64) is None)
del Config.PROFILES["bot_public"]["send"]

# --- a main with no public profile ------------------------------------------------------------
del Config.PROFILES["bot_main"]["public_profile"]
main = profiles.resolve_profile("dm_owner", OWNER, "dm")
check("a profile that names no public profile registers nobody", profiles.register_outreach(main, "e" * 64) == ""
      and db.registered_inbox("e" * 64) is None)

print(f"\n{'ALL OK' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
