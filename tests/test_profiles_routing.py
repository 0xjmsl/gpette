"""Which profile answers (modules/profiles.py): a group is matched by its own id and
never by its sender, what a group in no profile gets (`groups`), and the
`admin` / `admin_profile` / `menu` keys. No model, no XMTP.

    python tests/test_profiles_routing.py

Exit code 1 if any check fails.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("GPETTE_CONFIG", os.path.join(tempfile.mkdtemp(), "none.toml"))

from config import Config  # noqa: E402

Config.DATA_DIR = tempfile.mkdtemp(prefix="gpette_routing_")
Config.CLAUDE_CODE_GROUP_CWD = os.path.join(Config.DATA_DIR, "groups")
Config.INBOXES = {}
Config.PROFILES = {
    "bot_main": {"members": ["owner", "f" * 64], "admin": True, "menu": True, "gate": "open",
                 "allowed_tools": ["Read"], "workdir": os.path.join(Config.DATA_DIR, "main")},
    "bot_public": {"members": ["guest"], "admin_profile": "bot_main", "admin_inbox": "f" * 64},
    "club": {"members": ["group_listed"], "gate": "open"},
    "old_style": {"members": ["oldowner"], "admin": True},
    "quiet_admin": {"members": ["quiet"], "admin": True, "menu": False},
}

from modules import admin_menu, profiles  # noqa: E402
from modules.gates import get_gate  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def msg(text, sender):
    return {"conversationId": "c", "senderInboxId": sender, "contentType": "text",
            "content": {"type": "text", "value": text}}


# --- a DM is matched by its sender -------------------------------------------------
p = profiles.resolve_profile("dm1", "owner", "dm")
check("the owner's DM gets the owner's profile", p.name == "bot_main", p.name)
p = profiles.resolve_profile("dm2", "nobody", "dm")
check("a DM from someone in no profile gets the default profile", p.name == "default", p.name)

# --- a group is matched by its id, never by its sender ------------------------------
p = profiles.resolve_profile("group_listed", "nobody", "group")
check("a listed group gets its profile whoever writes", p.name == "club", p.name)
p = profiles.resolve_profile("group_listed", "owner", "group")
check("a listed group keeps its profile when the owner writes", p.name == "club", p.name)
p = profiles.resolve_profile("group_unlisted", "owner", "group")
check("the owner writing in an unlisted group does NOT bring the owner's profile",
      p.name == "group", p.name)
check("that group runs with no tools", p.no_tools and not p.allowed_tools, p.allowed_tools)
check("and not in the owner's folder", "main" not in (p.workdir or ""), p.workdir)

# --- what a group in no profile gets -------------------------------------------------
Config.CLAUDE_CODE_GROUPS = "sandbox"
p = profiles.resolve_profile("g1", "owner", "group")
check("groups = sandbox: the tool-less group profile, top-level gate",
      p.name == "group" and p.gate == (Config.GATE_KIND or "open"), p.gate)
Config.CLAUDE_CODE_GROUPS = "none"
p = profiles.resolve_profile("g1", "owner", "group")
check("groups = none: the group meets the `none` gate", p.gate == "none", p.gate)
verdict = get_gate(p.gate).check_message(msg("hi", "owner"), kind="group")
check("which drops the message", verdict.get("action") == "reject", verdict)
p = profiles.resolve_profile("group_listed", "owner", "group")
check("groups = none does not touch a listed group", p.name == "club" and p.gate == "open", p.gate)
p = profiles.resolve_profile("dm2", "nobody", "dm")
check("groups = none does not touch DMs", p.gate != "none" or (Config.GATE_KIND == "none"), p.gate)

Config.INBOXES = {"alpha": {"url": "http://127.0.0.1:1", "groups": "sandbox"},
                  "beta": {"url": "http://127.0.0.1:2"}}
check("an inbox's own `groups` wins over the top-level one", profiles.groups_mode("alpha") == "sandbox")
check("an inbox without one follows the top-level one", profiles.groups_mode("beta") == "none")
check("an unknown value falls back to sandbox",
      profiles.groups_mode("alpha") == "sandbox" and
      (Config.INBOXES["alpha"].__setitem__("groups", "banana") or profiles.groups_mode("alpha")) == "sandbox")
try:
    profiles.check_config()
    check("the boot check refuses an unknown `groups` on an inbox", False)
except ValueError as e:
    check("the boot check refuses an unknown `groups` on an inbox", "alpha" in str(e), e)
Config.INBOXES = {}
Config.CLAUDE_CODE_GROUPS = "banana"
try:
    profiles.check_config()
    check("the boot check refuses an unknown top-level `groups`", False)
except ValueError as e:
    check("the boot check refuses an unknown top-level `groups`", "banana" in str(e), e)
Config.CLAUDE_CODE_GROUPS = "sandbox"

# --- nothing_matched: what answers when no profile did --------------------------------
check("unset: the config does not say", profiles.nothing_matched() == (False, "", False))
Config.NOTHING_MATCHED = ""
p = profiles.resolve_profile("dm2", "nobody", "dm")
check("nothing_matched = \"\": a stranger's DM is not answered", p.gate == "none", p.gate)
p = profiles.resolve_profile("dm1", "owner", "dm")
check("someone in a profile is untouched by it", p.name == "bot_main" and p.gate == "open", p.gate)
Config.NOTHING_MATCHED = "bot_public"
p = profiles.resolve_profile("dm2", "nobody", "dm")
check("nothing_matched = a profile: a stranger's DM gets that profile",
      p.name == "bot_public" and not p.send and not p.admin, p.name)
p = profiles.resolve_profile("group_unlisted", "nobody", "group")
check("it does not answer groups unless told to", p.name == "group", p.name)
Config.NOTHING_MATCHED_GROUPS = True
p = profiles.resolve_profile("group_unlisted", "nobody", "group")
check("nothing_matched_groups = true: an unlisted group gets that profile too", p.name == "bot_public", p.name)
p = profiles.resolve_profile("group_listed", "nobody", "group")
check("a listed group still gets its own profile", p.name == "club", p.name)
Config.NOTHING_MATCHED_GROUPS = False
Config.INBOXES = {"alpha": {"url": "http://127.0.0.1:1", "nothing_matched": ""},
                  "beta": {"url": "http://127.0.0.1:2", "nothing_matched": "club"}}
check("an inbox's own nothing_matched is the one that counts for it",
      profiles.nothing_matched("alpha") == (True, "", False) and profiles.nothing_matched("beta") == (True, "club", False))
Config.NOTHING_MATCHED = "ghost"
try:
    profiles.check_config()
    check("the boot check refuses a nothing_matched that names no profile", False)
except ValueError as e:
    check("the boot check refuses a nothing_matched that names no profile", "ghost" in str(e), e)
p = profiles.resolve_profile("dm9", "nobody", "dm")
Config.INBOXES = {}
p = profiles.resolve_profile("dm9", "nobody", "dm")
check("a profile that disappears while running means silence, not a guess", p.gate == "none", (p.name, p.gate))
Config.NOTHING_MATCHED = None

# --- admin = may be woken; menu is its own key ---------------------------------------
main = profiles.resolve_profile("dm1", "owner", "dm")
pub = profiles.resolve_profile("dm3", "guest", "dm")
old = profiles.resolve_profile("dm4", "oldowner", "dm")
quiet = profiles.resolve_profile("dm5", "quiet", "dm")
check("admin + menu: both on", main.admin and main.menu)
check("a public profile is neither", not pub.admin and not pub.menu)
check("admin grants no tools: the profile keeps its own list", main.allowed_tools == ["Read"], main.allowed_tools)
check("admin = true with no `menu` key keeps the menu (configs written before the key)",
      old.admin and old.menu and old.on_error == "describe")
check("admin = true, menu = false: wakeable, no menu, generic errors",
      quiet.admin and not quiet.menu and quiet.on_error == "generic")
_, consumed, _ = admin_menu.handle(msg("/menu", "owner"), main)
check("/menu works with menu = true", consumed)
_, consumed, _ = admin_menu.handle(msg("/menu", "quiet"), quiet)
check("/menu is ordinary text with menu = false, admin or not", not consumed)
check("the public profile names its main", pub.admin_profile == "bot_main", pub.admin_profile)

# --- the admin link: checked at boot and at each use ----------------------------------
profiles.check_config()
check("a good config passes the boot check", True)
check("the public profile's [ADMIN_TURN] goes to its main",
      profiles.admin_target(pub) == ("bot_main", None), profiles.admin_target(pub))
t, why = profiles.admin_target("club")
check("a profile with no admin_profile has no target", t is None and "no admin_profile" in why, why)

Config.PROFILES["bot_public"]["admin_profile"] = "ghost"
t, why = profiles.admin_target("bot_public")
check("a target that does not exist is an error, not a crash", t is None and "ghost" in why, why)
try:
    profiles.check_config()
    check("and the boot check refuses it", False)
except ValueError as e:
    check("and the boot check refuses it", "ghost" in str(e), e)

Config.PROFILES["bot_public"]["admin_profile"] = "club"
t, why = profiles.admin_target("bot_public")
check("a target without admin = true is an error", t is None and "admin = true" in why, why)

Config.PROFILES["bot_public"]["admin_profile"] = "bot_public"
t, why = profiles.admin_target("bot_public")
check("a profile cannot be its own admin_profile", t is None and "itself" in why, why)

# the config changes under a running gpette: the next use sees it
Config.PROFILES["bot_public"]["admin_profile"] = "bot_main"
check("the link works again", profiles.admin_target("bot_public")[0] == "bot_main")
Config.PROFILES["bot_main"]["admin"] = False
t, why = profiles.admin_target("bot_public")
check("admin switched off while running is caught at the next use", t is None and "admin = true" in why, why)
del Config.PROFILES["bot_main"]
t, why = profiles.admin_target("bot_public")
check("the main removed while running is caught at the next use", t is None and "not in the config" in why, why)

print(f"\n{'ALL OK' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
