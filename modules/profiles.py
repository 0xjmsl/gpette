"""Profiles: which environment answers which conversation.

A profile is declared in gpette.toml as `[profiles.<name>]` with a `members`
list (group conversation ids and/or sender inbox ids). A DM is matched by its
sender's inbox id. A group is matched by its own conversation id ONLY, never by
who wrote in it: a group gets a profile when someone lists its id. Unmatched:

- a DM uses the `default` profile, built from the top-level settings;
- a group follows `[claude_code] groups` (or `[inboxes.<name>] groups` for the
  inbox that received it): `sandbox` = its own empty folder under
  `[claude_code] group_workdir`, no tools, the group prompt; `default` = the
  top-level folder and tools; `none` = not answered at all.

`nothing_matched` says it outright for one inbox (`[inboxes.<name>]`, or `[daemon]`
when there is one daemon): `""` = whoever matched no profile is not answered; a
profile name = that profile answers them, behind its own gate. It covers DMs;
`nothing_matched_groups = true` makes it cover groups no profile lists as well
(off unless written: a group is added by hand). The older way to say "this
profile answers everyone at its inbox", a profile bound to an inbox with no
`members`, still works, under the same rule for groups.

Every profile key is optional and falls back to the top-level value:

    provider            claude-code | claude | openai | gemini | lmstudio | ollama
    model               model name for that provider
    workdir             Claude Code working folder (its CLAUDE.md)
    allowed_tools       Claude Code tools, list or "A,B"; [] = no tools at all
    system_prompt       inline prompt: appended for Claude Code, the system prompt
                        for API providers
    system_prompt_file  same, read from a file (wins over system_prompt)
    sessions            per_conversation | shared (every member resumes one session) |
                        per_account (one session per sender's owner: [sessions] owner_resolver,
                        e.g. the account behind an inbox, so a person's inboxes share one
                        conversation memory; groups stay per conversation) |
                        fresh (every turn is a new session that starts with the summary and
                        the latest chat messages; nothing is resumed, so a changed CLAUDE.md
                        is read on the very next turn)
    gate               open | password | <plug-in module>
    tools               Python tools (./tools) an API provider may call; [] = none
    max_turns, timeout_secs   Claude Code limits
    privacy             true = never reveal paths, user names, machine or config details
    inherit_user_settings   true = load the host user's Claude Code settings, CLAUDE.md and
                        memory (owner-only; default from [claude_code], never for groups)
    admin               true = other profiles may wake this one ([ADMIN_TURN]). It grants
                        nothing else: tools and folder stay what the profile says
    admin_profile       the profile this one's [ADMIN_TURN] wakes ('' = none). It must exist
                        and have admin = true: checked at boot and again at each use
    admin_inbox         with admin_profile: the inbox id the main is woken FOR. The wake-up
                        goes to the main's DM with exactly this inbox (opened through the
                        daemon if it does not exist yet). It must be one of the main's
                        members. Nothing is looked up from history
    admin_turns_per_inbox_per_day   on the asking profile: requests one person may raise a day
    admin_turns_per_day             on the woken profile: requests it takes a day (0 = no limit;
                        defaults in [admin_turn], modules/admin_turn.py)
    per_inbox           true = `workdir` is a parent: each person who writes gets their own
                        subfolder `<workdir>/<their inbox id>/` and runs fenced inside it. The
                        profile's instructions are then `<workdir>/CLAUDE.md`, one file for all
    knowledge           a folder every run of this profile may also read (Claude Code --add-dir)
    public_profile      the profile that answers the people THIS profile writes to first
                        ([SEND_XMTP_MESSAGE]). It must be per_inbox and hold no power (no admin,
                        send, menu, inherit_user_settings or "*" tools): the only kind of
                        profile gpette ever puts someone in by itself
    send                true = this profile may write to other inboxes and registered groups
                        with [SEND_XMTP_MESSAGE] (modules/send_block.py). Off unless written;
                        Claude Code profiles only
    menu                true = /menu (model, timers, session, status) and full error text.
                        (Until `menu` is written, `admin = true` still turns it on.)
    on_error            describe | generic (default: describe with the menu, else generic)
    error_reply         generic: what people see when the model fails

Defaults are read from Config at resolve time, so runtime edits apply.
"""
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from config import Config, ALL_CLAUDE_TOOLS
from modules import admin_menu, inboxes

DEFAULT = 'default'
GROUP = 'group'


@dataclass
class Profile:
    name: str
    provider: Optional[str] = None        # None = the server's active service
    model: str = ''                       # '' = the provider's configured model
    workdir: str = ''
    per_group_workdir: bool = False       # workdir is a parent: one subfolder per group
    allowed_tools: list = field(default_factory=list)
    no_tools: bool = False                # fence: no built-in tools, no MCP, no user settings
    disallowed_tools: list = field(default_factory=list)
    append_prompt: str = ''               # Claude Code --append-system-prompt
    system_prompt: Optional[str] = None   # API providers; None = built-in prompt
    sessions: str = 'per_conversation'
    gate: str = 'open'
    tools: Optional[list] = None          # API providers' Python tools; None = all
    max_turns: int = 0
    timeout_secs: int = 0
    privacy: bool = True                  # never reveal paths, usernames, machine or config details
    inherit_user_settings: bool = False   # load the host user's Claude Code settings/CLAUDE.md/memory
    session_owner: str = ''               # per_account: who this message's session belongs to
    transcripts: bool = False             # write conversations/<owner>/ files the bot can read (modules/transcripts.py)
    admin: bool = False                   # other profiles may wake this one with [ADMIN_TURN]; nothing more
    admin_profile: str = ''               # the profile this one's [ADMIN_TURN] wakes ('' = none)
    menu: bool = False                    # /menu, and errors described in full (modules/admin_menu.py)
    send: bool = False                    # may use [SEND_XMTP_MESSAGE] (modules/send_block.py)
    per_inbox: bool = False               # workdir is a parent: one subfolder per person (their inbox id)
    folder_key: str = ''                  # per_inbox: whose subfolder this run uses
    knowledge: str = ''                   # a folder every run may also read (--add-dir)
    public_profile: str = ''              # who answers the people this profile writes to first
    via_inbox_list: bool = False          # this message matched through the inbox list
    on_error: str = 'generic'             # describe | generic: how a failed model run is answered
    error_reply: str = ''                 # generic: the text sent ('' = the built-in one)
    inbox: str = ''                       # [inboxes.<name>] this profile speaks through ('' = any)

    def session_key(self, conversation_id):
        """Where this conversation's Claude Code session id is stored."""
        if self.sessions == 'shared':
            return f'profile:{self.name}'
        if self.sessions == 'per_account' and self.session_owner:
            return f'owner:{self.session_owner}'
        return conversation_id

    def tool_allowed(self, tool_name):
        return self.tools is None or tool_name in self.tools


def _norm(value):
    return str(value or '').strip().lower().removeprefix('0x')


def _tool_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [t.strip() for t in value.split(',') if t.strip()]
    return [str(t).strip() for t in value if str(t).strip()]


def _read_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError as e:
        logging.error(f"profiles: cannot read prompt file {path}: {e}")
        return None


GROUP_MODES = ('sandbox', 'default', 'none')


def groups_mode(inbox=None):
    """What a group in no profile gets: `[inboxes.<inbox>] groups` when the inbox
    that received it sets one, else `[claude_code] groups`. Unknown = sandbox."""
    entry = (Config.INBOXES or {}).get(inbox) if inbox else None
    mode = (entry.get('groups') if isinstance(entry, dict) else None) \
        or getattr(Config, 'CLAUDE_CODE_GROUPS', 'sandbox')
    mode = str(mode or 'sandbox').strip()
    return mode if mode in GROUP_MODES else 'sandbox'


def nothing_matched(inbox=None):
    """(set, profile name, groups) for the inbox that received a message: what
    answers when no profile matched. `set` is False when the config does not say
    (then the default profile / the groups mode apply); a name of '' = nobody."""
    entry = (Config.INBOXES or {}).get(inbox) if inbox else None
    if isinstance(entry, dict) and 'nothing_matched' in entry:
        return True, str(entry.get('nothing_matched') or '').strip(), bool(entry.get('nothing_matched_groups'))
    groups = bool(entry.get('nothing_matched_groups')) if isinstance(entry, dict) and 'nothing_matched_groups' in entry \
        else bool(getattr(Config, 'NOTHING_MATCHED_GROUPS', False))
    top = getattr(Config, 'NOTHING_MATCHED', None)
    if top is not None:              # an inbox that sets none of its own follows [daemon]
        return True, str(top or '').strip(), groups
    return False, '', groups


def _base(name, is_group, mode=None):
    """The top-level settings as a profile (the `default` one, or the `group` sandbox).
    `mode` is the groups mode to use for a group (None = `[claude_code] groups`)."""
    mode = mode or groups_mode()
    p = Profile(
        name=name,
        model=Config.CLAUDE_CODE_MODEL or '',
        max_turns=int(Config.CLAUDE_CODE_MAX_TURNS or 0),
        timeout_secs=int(Config.CLAUDE_CODE_TIMEOUT or 0),
        gate=Config.GATE_KIND or 'open',
        privacy=bool(getattr(Config, 'PRIVACY', True)),
        # groups never inherit (they are the tool-less sandbox)
        inherit_user_settings=(not is_group) and bool(getattr(Config, 'CLAUDE_CODE_INHERIT_USER_SETTINGS', False)),
        transcripts=bool(getattr(Config, 'CLAUDE_CODE_TRANSCRIPTS', False)),
    )
    # [claude_code] groups = "default": a group in no profile gets the top-level
    # environment (its folder and tools) instead of the empty sandbox; still one
    # session per group, never the host user's settings.
    if is_group and mode == 'default':
        p.workdir = Config.CLAUDE_CODE_CWD
        p.allowed_tools = _tool_list(Config.CLAUDE_CODE_ALLOWED_TOOLS) or []
        if not p.allowed_tools or '*' in p.allowed_tools:
            p.allowed_tools = [t for t in p.allowed_tools if t != '*']
            if not p.allowed_tools:
                p.no_tools = True
                p.disallowed_tools = _tool_list(ALL_CLAUDE_TOOLS)
        # the sandbox's group prompt says "you have no tools"; here the group has them
        base_prompt = Config.CLAUDE_CODE_SYSTEM_PROMPT or ''
        p.append_prompt = (base_prompt + ' ' if base_prompt else '') + (
            'This is a group chat: each message starts with the sender\'s name.')
        p.sessions = 'per_conversation'
    elif is_group:
        p.workdir = Config.CLAUDE_CODE_GROUP_CWD
        p.per_group_workdir = True
        p.no_tools = True
        p.disallowed_tools = _tool_list(Config.CLAUDE_CODE_GROUP_DISALLOWED_TOOLS)
        p.append_prompt = Config.CLAUDE_CODE_GROUP_SYSTEM_PROMPT or ''
        p.sessions = 'per_conversation'     # each group keeps its own session
        if mode == 'none':
            p.gate = 'none'                 # groups = "none": a group nobody listed is not answered
    else:
        p.workdir = Config.CLAUDE_CODE_CWD
        p.allowed_tools = _tool_list(Config.CLAUDE_CODE_ALLOWED_TOOLS) or []
        if not p.allowed_tools:             # [] = no tools: the same fence as groups
            p.no_tools = True
            p.disallowed_tools = _tool_list(ALL_CLAUDE_TOOLS)
        p.append_prompt = Config.CLAUDE_CODE_SYSTEM_PROMPT or ''
        p.sessions = Config.SESSION_ROUTING or 'per_conversation'
    if Config.SYSTEM_PROMPT_FILE:
        p.system_prompt = _read_file(Config.SYSTEM_PROMPT_FILE)
    p.error_reply = str(getattr(Config, 'ERROR_REPLY', '') or '')
    return admin_menu.apply_overrides(p)


def _from_config(name, raw):
    p = _base(name, is_group=False)
    if raw.get('provider'):
        p.provider = raw['provider']
        if p.provider != 'claude-code':
            p.model = ''                    # [claude_code] model is not this provider's
    if raw.get('model'):
        p.model = raw['model']
    if raw.get('workdir'):
        p.workdir = raw['workdir']
    if 'allowed_tools' in raw:
        tools = _tool_list(raw['allowed_tools'])
        p.allowed_tools = tools
        # [] = no tools: the same fence as groups. A non-empty list lifts the
        # fence the top-level `allowed_tools = []` may have set in _base.
        p.no_tools = not tools
        p.disallowed_tools = _tool_list(ALL_CLAUDE_TOOLS) if not tools else []
    prompt = None
    if raw.get('system_prompt_file'):
        prompt = _read_file(raw['system_prompt_file'])
    if prompt is None and raw.get('system_prompt'):
        prompt = raw['system_prompt']
    if prompt is not None:
        p.system_prompt = prompt
        p.append_prompt = prompt
    if raw.get('sessions'):
        p.sessions = raw['sessions']
    if raw.get('gate'):
        p.gate = raw['gate']
    if 'privacy' in raw:
        p.privacy = bool(raw['privacy'])
    if 'inherit_user_settings' in raw:
        p.inherit_user_settings = bool(raw['inherit_user_settings'])
    if 'transcripts' in raw:
        p.transcripts = bool(raw['transcripts'])
    if 'tools' in raw:
        p.tools = _tool_list(raw['tools'])
    if raw.get('max_turns') is not None:
        p.max_turns = int(raw['max_turns'])
    if raw.get('timeout_secs') is not None:
        p.timeout_secs = int(raw['timeout_secs'])
    if p.sessions not in ('per_conversation', 'shared', 'per_account', 'fresh'):
        logging.warning(f"profile '{name}': sessions='{p.sessions}' unknown, using per_conversation")
        p.sessions = 'per_conversation'
    if 'admin' in raw:
        p.admin = bool(raw['admin'])
    p.admin_profile = str(raw.get('admin_profile') or '').strip()
    p.send = bool(raw.get('send'))
    p.per_inbox = bool(raw.get('per_inbox'))
    p.knowledge = str(raw.get('knowledge') or '').strip()
    p.public_profile = str(raw.get('public_profile') or '').strip()
    # `menu` is its own key. A config written before it existed has only
    # `admin = true`, which used to mean the menu: it still does until `menu` is set.
    p.menu = bool(raw['menu']) if 'menu' in raw else p.admin
    p.on_error = str(raw.get('on_error') or ('describe' if p.menu else 'generic')).strip()
    if p.on_error not in ('describe', 'generic'):
        logging.warning(f"profile '{name}': on_error='{p.on_error}' unknown, using generic")
        p.on_error = 'generic'
    p.error_reply = str(raw.get('error_reply') or '')
    p.inbox = _inbox_of(raw)
    return admin_menu.apply_overrides(p)


def base_profile(is_group):
    """`default` or the `group` sandbox, for callers that have no sender to match."""
    return _base(GROUP if is_group else DEFAULT, is_group)


def _inbox_of(raw):
    return str(raw.get('inbox') or '').strip()


def _match_in(member_id, names):
    key = _norm(member_id)
    if not key:
        return None
    for name in names:
        if any(_norm(m) == key for m in (Config.PROFILES[name].get('members') or [])):
            return name
    return None


def _match(member_id):
    return _match_in(member_id, list(Config.PROFILES or {}))


# --- the inbox list: people this bot wrote to first ------------------------------------
# One table in the conversation store (registered_inboxes). It is read at every
# message, never cached: a row added or removed under a running gpette counts at once.

_store = None


def _db():
    global _store
    if _store is None:
        from models.conversation_db import ConversationDB
        _store = ConversationDB()
    return _store


# The tools a profile gpette hands out by itself may have: the ones whose reach gpette
# can limit to one person's folder. A shell (Bash, PowerShell) reaches the whole machine.
PUBLIC_TOOLS = ('Read', 'Glob', 'Grep', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit')


def public_ok(name):
    """None when `name` may be given to someone by gpette itself, else the reason.
    Such a profile keeps people apart (per_inbox) and holds no power."""
    raw = (Config.PROFILES or {}).get(name)
    if not isinstance(raw, dict):
        return f"profile '{name}' is not in the config"
    if not raw.get('per_inbox'):
        return f"profile '{name}' is not per_inbox"
    if not str(raw.get('workdir') or '').strip():
        return f"profile '{name}' is per_inbox and has no workdir"
    for key in ('admin', 'send', 'menu', 'inherit_user_settings'):
        if raw.get(key):
            return f"profile '{name}' has {key} = true"
    if 'allowed_tools' not in raw:
        return f"profile '{name}' does not set allowed_tools (it would take the top-level ones)"
    for t in (_tool_list(raw.get('allowed_tools')) or []):
        tool = t.partition('(')[0].strip()
        if tool == '*':
            return f"profile '{name}' allows every tool (\"*\")"
        if tool not in PUBLIC_TOOLS:
            return (f"profile '{name}' allows {tool}: a public profile may only have the file tools "
                    f"({', '.join(PUBLIC_TOOLS)}), which gpette can keep inside one person's folder")
    return None


def _registered(sender_inbox_id, inbox=None):
    """The public profile the inbox list gives this sender, or None."""
    key = _norm(sender_inbox_id)
    if not key:
        return None
    try:
        row = _db().registered_inbox(key, inbox or '')
    except Exception as e:
        logging.error(f"profiles: inbox list not readable ({e}); nobody matched through it")
        return None
    if not row:
        return None
    name = row.get('profile')
    why = public_ok(name)
    if why:
        logging.error(f"profiles: inbox {key[:12]}... is on the list for '{name}', but {why}; not used")
        return None
    return name


def register_outreach(profile, inbox_id):
    """`profile` just wrote first to `inbox_id`. Put that inbox on the list for the
    profile's `public_profile` and make its folder. Returns the sentence to add to
    the send's result ('' when nothing new was made).

    Nothing happens when the profile names no public profile, when that profile
    could not pass `public_ok`, or when the inbox already belongs to a profile in
    the config (the owner, a box): the list never moves anyone out of a profile."""
    target = getattr(profile, 'public_profile', '') or ''
    key = _norm(inbox_id)
    if not target or not key:
        return ''
    why = public_ok(target)
    if why:
        logging.error(f"profiles: {getattr(profile, 'name', '?')} wrote to {key[:12]}..., "
                      f"not put on the list: {why}")
        return ''
    own = getattr(profile, 'inbox', '') or ''
    if _pick(None, key, False, own or None):
        return ''
    _db().register_inbox(key, target, 'outreach', own_inbox=own, added_by=getattr(profile, 'name', ''))
    folder = os.path.join(Config.PROFILES[target]['workdir'], key)
    if os.path.isdir(folder):
        return ''
    os.makedirs(folder, exist_ok=True)
    logging.info(f"profiles: new public folder for {key[:12]}... in profile '{target}'")
    return f"New public folder created for inbox {key}."


def _pick_for(conversation_id, sender_inbox_id, is_group, inbox=None):
    """`_pick` with the inbox list looked up (a DM only: a group is never on it)."""
    registered = None if is_group else _registered(sender_inbox_id, inbox)
    name = _pick(conversation_id, sender_inbox_id, is_group, inbox, registered)
    return name, bool(registered and name == registered)


def _pick(conversation_id, sender_inbox_id, is_group, inbox=None, registered=None):
    """The profile name for a chat, or None (= default / the groups mode).

    A DM is matched by its sender. A group is matched by its own id and never
    by its sender: who writes in a group does not change which profile answers.

    No inbox known (one daemon, or no [inboxes]): every profile. A known inbox
    (the configured name of the inbox that received the message, or that owns
    the conversation): first the profiles bound to it (the id, then a bound
    profile with no members, which answers everyone who writes to that inbox),
    then the profiles bound to no inbox.

    `registered` is the public profile the inbox list gives this sender (a DM):
    it comes after a `members` match, so the list never moves anyone out of a
    profile, and before "everyone at this inbox"."""
    profiles = Config.PROFILES or {}
    conversation_id = inboxes.base_conversation(conversation_id)   # `<group>@<inbox>` -> the group id
    key = conversation_id if is_group else sender_inbox_id
    if not inbox:
        return _match(key) or registered
    bound = [n for n, r in profiles.items() if _inbox_of(r) == inbox]
    unbound = [n for n, r in profiles.items() if not _inbox_of(r)]
    everyone = next((n for n in bound if not profiles[n].get('members')), None)
    if is_group and not nothing_matched(inbox)[2]:
        everyone = None              # "everyone at this inbox" means DMs, unless groups are switched on
    return _match_in(key, bound) or registered or everyone or _match_in(key, unbound)


_owner_fn = None
_owner_loaded = False
_owner_cache = {}


def owner_for(inbox_id):
    """Who a sender is, for `sessions = "per_account"`: the resolver plug-in's answer
    (`[sessions] owner_resolver`, a module with owner_for(inbox_id) -> str | None, e.g.
    the account an inbox belongs to), else the inbox itself. Cached per inbox."""
    global _owner_fn, _owner_loaded
    key = _norm(inbox_id)
    if not key:
        return ''
    if key in _owner_cache:
        return _owner_cache[key]
    if not _owner_loaded:
        _owner_loaded = True
        path = (getattr(Config, 'SESSION_OWNER_RESOLVER', '') or '').strip()
        if path:
            try:
                import importlib
                _owner_fn = getattr(importlib.import_module(path), 'owner_for')
            except Exception as e:
                logging.error(f"profiles: owner resolver '{path}' failed to load: {e}")
    owner = None
    if _owner_fn is not None:
        try:
            owner = _owner_fn(inbox_id)
        except Exception as e:
            logging.warning(f"profiles: owner resolver failed for {key[:12]}...: {e}")
            return f'inbox:{key}'                 # not cached: try again next message
    owner = str(owner).strip().lower() if owner else f'inbox:{key}'
    _owner_cache[key] = owner
    return owner


def resolve_profile(conversation_id, sender_inbox_id, kind, inbox_id=None):
    """The profile for a message: its group id first, then its sender, else
    `default` (DM) or the `group` sandbox (group). `inbox_id` is the inbox that
    received it (the envelope's `inboxId`); with [inboxes] it limits the match
    to the profiles bound to that inbox first (modules/inboxes.py)."""
    is_group = kind == 'group'
    inbox = None
    if inboxes.configured():
        inbox = inboxes.split(conversation_id)[1] or (inboxes.name_of(inbox_id) if inbox_id else None)
        if inbox:
            inboxes.note(conversation_id, inbox)
        else:
            inbox = inboxes.of_conversation(conversation_id)
    name, via_list = _pick_for(conversation_id, sender_inbox_id, is_group, inbox)
    if not name:
        said, fallback, with_groups = nothing_matched(inbox)
        if said and (with_groups or not is_group):
            if fallback in (Config.PROFILES or {}):
                name = fallback
            else:
                if fallback:             # named a profile that is gone: silence, never a guess
                    logging.error(f"profiles: nothing_matched = '{fallback}' is not a profile, not answering")
                profile = _base(GROUP if is_group else DEFAULT, is_group, groups_mode(inbox))
                profile.gate = 'none'
                remember(conversation_id, profile.name, kind, '', sender_inbox_id)
                return profile
    if name:
        profile = _from_config(name, Config.PROFILES[name])
    else:
        profile = _base(GROUP if is_group else DEFAULT, is_group, groups_mode(inbox))
    if via_list:
        profile.via_inbox_list = True
        profile.gate = 'open'            # we wrote to them first: their answer needs no password
    if profile.per_inbox and not is_group:
        profile.folder_key = _norm(sender_inbox_id)
    if profile.sessions == 'per_account':
        if is_group:
            profile.sessions = 'per_conversation'    # a group has many senders: keep one session per group
        else:
            profile.session_owner = owner_for(sender_inbox_id)
    remember(conversation_id, profile.name, kind, profile.session_owner, sender_inbox_id)
    return profile


# --- conversation -> profile, for messages that carry no sender ---------------------
# Tool results arrive with a conversation id only. The last resolution for that
# conversation is remembered here; after a restart the DM's last human sender is
# looked up in the stored history instead. Timer fires do NOT come through here:
# a timer carries its own profile and inbox (profile_for_timer below).

_last = {}
_last_lock = threading.Lock()


def remember(conversation_id, name, kind, owner='', sender=''):
    if conversation_id:
        with _last_lock:
            _last[conversation_id] = (name, kind, owner)
            _last_sender[conversation_id] = _norm(sender)


_last_sender = {}


# --- timers: a fire runs as what the timer says ---------------------------------------
# A timer row holds the conversation its reply goes to, the profile it was set in
# and the inbox that set it. Nothing is looked up at fire time; the stored values
# are only checked against the config, and a timer that fails the check does not fire.

def timer_binding(conversation_id):
    """(profile name, inbox) of the turn being answered in this conversation, for
    a timer set during it. None when this process has answered nothing there."""
    with _last_lock:
        known = _last.get(conversation_id)
        sender = _last_sender.get(conversation_id, '')
    if not known:
        return None
    return known[0], sender


def check_timer_binding(name, owner_inbox, conversation_id, is_group):
    """None when a timer may run as profile `name` in this conversation, else the
    reason it may not. The rule: `name` is the profile this chat resolves to today.
    A DM: the profile its inbox is a member of (else `default`), so the inbox a
    timer messages is always one linked to its profile. A group: the group's own
    profile, else the `group` sandbox (a group is never matched by a sender)."""
    name = (name or '').strip()
    if not name:
        return "it has no profile (bind it: scripts/timer.py bind)"
    if name not in (Config.PROFILES or {}) and name not in (DEFAULT, GROUP):
        return f"its profile '{name}' is not in the config"
    inbox = None
    if inboxes.configured():
        bound = _inbox_of((Config.PROFILES or {}).get(name) or {})
        inbox = inboxes.split(conversation_id)[1] or inboxes.of_conversation(conversation_id) or bound or None
        if bound and inbox != bound:
            return f"this chat belongs to inbox '{inbox}', but profile '{name}' speaks through '{bound}'"
    if not is_group and not _norm(owner_inbox):
        return "it has no inbox (a timer in a DM reports to one inbox)"
    expected = _pick_for(conversation_id, owner_inbox, is_group, inbox)[0] or (GROUP if is_group else DEFAULT)
    if expected != name:
        who = 'this group' if is_group else f"inbox {_norm(owner_inbox)[:12]}..."
        return f"{who} belongs to profile '{expected}', not '{name}'"
    return None


def profile_for_timer(timer, is_group):
    """(profile, None) for a timer fire, or (None, reason) when it must not run."""
    name = (timer.get('profile') or '').strip()
    owner_inbox = timer.get('owner_inbox') or ''
    conversation_id = timer.get('conversation_id')
    reason = check_timer_binding(name, owner_inbox, conversation_id, is_group)
    if reason:
        return None, reason
    if name in (Config.PROFILES or {}):
        profile = _from_config(name, Config.PROFILES[name])
    else:
        profile = _base(name, is_group)
    if profile.inbox:
        inboxes.note(conversation_id, profile.inbox)    # its reply leaves through that inbox
    if profile.per_inbox and not is_group:
        profile.folder_key = _norm(owner_inbox)
    if profile.sessions == 'per_account':
        if is_group:
            profile.sessions = 'per_conversation'
        else:
            profile.session_owner = owner_for(owner_inbox)
    # The fire's own tool results, and a timer set during the fire, see this.
    remember(conversation_id, profile.name, 'group' if is_group else 'dm',
             profile.session_owner, owner_inbox)
    return profile, None


# --- admin turns: who a profile's [ADMIN_TURN] wakes -----------------------------------

def admin_target(profile_or_name):
    """(target profile name, None) for the profile a `[ADMIN_TURN]` from this one
    wakes, or (None, reason). Asked at every use, not only at boot: the config can
    change under a running gpette, and a bad link must answer, not crash."""
    name = getattr(profile_or_name, 'name', profile_or_name)
    raw = (Config.PROFILES or {}).get(name)
    if not isinstance(raw, dict):
        return None, f"profile '{name}' is not in the config"
    target = str(raw.get('admin_profile') or '').strip()
    if not target:
        return None, f"profile '{name}' has no admin_profile"
    if target == name:
        return None, f"profile '{name}' names itself as its admin_profile"
    target_raw = (Config.PROFILES or {}).get(target)
    if not isinstance(target_raw, dict):
        return None, f"admin_profile '{target}' of profile '{name}' is not in the config"
    if not bool(target_raw.get('admin')):
        return None, f"admin_profile '{target}' of profile '{name}' does not have admin = true"
    return target, None


def last_sender(conversation_id):
    """The inbox of the person this process last answered in a conversation ('' = unknown)."""
    with _last_lock:
        return _last_sender.get(conversation_id, '')


_INBOX_ID = re.compile(r'^[0-9a-f]{64}$')
_main_chats = {}                     # (main profile, inbox) -> the DM it is woken in
_main_lock = threading.Lock()


def admin_link(profile_or_name):
    """(main profile, inbox id, None) for whom a `[ADMIN_TURN]` from this profile wakes
    and for which inbox, or (None, None, reason). Both come from the config
    (`admin_profile` + `admin_inbox`); nothing is guessed."""
    target, why = admin_target(profile_or_name)
    if why:
        return None, None, why
    name = getattr(profile_or_name, 'name', profile_or_name)
    raw = (Config.PROFILES or {}).get(name) or {}
    inbox = _norm(raw.get('admin_inbox')).removeprefix('0x')
    if not inbox:
        return None, None, f"profile '{name}' has admin_profile but no admin_inbox (the inbox its main is woken for)"
    if not _INBOX_ID.match(inbox):
        return None, None, f"admin_inbox of profile '{name}' is not an inbox id (64 hex characters)"
    members = [_norm(m).removeprefix('0x') for m in ((Config.PROFILES or {}).get(target) or {}).get('members') or []]
    if inbox not in members:
        return None, None, f"admin_inbox of profile '{name}' is not one of the members of '{target}'"
    return target, inbox, None


def main_conversation(target, inbox, call=None):
    """(conversation id, None) for the DM of main profile `target` with `inbox`: the
    chat an `[ADMIN_TURN]` wakes it in. Opened (or found) through the daemon of the
    inbox `target` speaks through, once, then remembered. Or (None, reason)."""
    key = (target, inbox)
    with _main_lock:
        known = _main_chats.get(key)
    if known:
        return _bind_main(known, target, inbox)
    raw = (Config.PROFILES or {}).get(target) or {}
    name = _inbox_of(raw) or None
    url = (inboxes.url_of(name) if name else '') or str(Config.XMTP_DAEMON_URL).rstrip('/')
    if call is None:
        from server.xmtp_http_bridge import daemon_call as call
    data, why = call(url, '/dm', {'inboxId': inbox})
    if why:
        return None, f"the DM of '{target}' with its admin_inbox could not be opened ({why})"
    conversation = (data or {}).get('conversationId')
    if not conversation:
        return None, f"the daemon opened no DM for '{target}' and its admin_inbox"
    if name:
        inboxes.note(conversation, name)                 # the main's answer leaves through its inbox
    with _main_lock:
        _main_chats[key] = conversation
    return _bind_main(conversation, target, inbox)


def _bind_main(conversation, target, inbox):
    """Tie the DM to its main the same way a message from `inbox` would (so a chat
    nobody wrote in yet still answers as `target`), and check it really does."""
    profile = resolve_profile(conversation, inbox, 'dm')
    if profile.name != target:
        return None, f"the DM with admin_inbox resolves to profile '{profile.name}', not '{target}'"
    return conversation, None


def check_config():
    """Refuse to start with an `admin_profile` that cannot be woken, or a `groups`
    value that means nothing. Warn about `admin = true` written before `menu`
    existed. Returns nothing; raises ValueError."""
    for name, raw in (Config.PROFILES or {}).items():
        if not isinstance(raw, dict):
            continue
        if str(raw.get('admin_profile') or '').strip():
            _, _, reason = admin_link(name)
            if reason:
                raise ValueError(f"[profiles.{name}]: {reason}")
        target = str(raw.get('public_profile') or '').strip()
        if target:
            why = public_ok(target)
            if why:
                raise ValueError(f"[profiles.{name}] public_profile = '{target}': {why}")
        if raw.get('per_inbox') and not str(raw.get('workdir') or '').strip():
            raise ValueError(f"[profiles.{name}] per_inbox = true needs a workdir (the parent of the per-person folders)")
        if raw.get('send') and (raw.get('provider') or Config.LLM_SERVICE) != 'claude-code':
            raise ValueError(f"[profiles.{name}] send = true needs provider claude-code "
                             f"(the result of a send resumes a Claude Code session)")
        if raw.get('admin') and 'menu' not in raw:
            logging.warning(
                f"[profiles.{name}] has admin = true and no `menu` key. `admin` now means "
                f"'other profiles may wake this one'; the /menu has its own key. It stays on "
                f"for this profile until you write menu = true (or false).")
    where = [('[daemon]', getattr(Config, 'NOTHING_MATCHED', None))]
    where += [(f'[inboxes.{n}]', e.get('nothing_matched')) for n, e in (Config.INBOXES or {}).items()
              if isinstance(e, dict) and 'nothing_matched' in e]
    for label, value in where:
        value = str(value or '').strip()
        if value and value not in (Config.PROFILES or {}):
            raise ValueError(f"{label} nothing_matched = '{value}', but there is no [profiles.{value}]")
    top = str(getattr(Config, 'CLAUDE_CODE_GROUPS', 'sandbox') or 'sandbox').strip()
    if top not in GROUP_MODES:
        raise ValueError(f"[claude_code] groups = '{top}' is not one of {', '.join(GROUP_MODES)}")
    for iname, entry in (Config.INBOXES or {}).items():
        mode = str(entry.get('groups') or '').strip() if isinstance(entry, dict) else ''
        if mode and mode not in GROUP_MODES:
            raise ValueError(f"[inboxes.{iname}] groups = '{mode}' is not one of {', '.join(GROUP_MODES)}")


def profile_for_conversation(conversation_id, kind=None, conversation_db=None):
    with _last_lock:
        known = _last.get(conversation_id)
    if known:
        name, known_kind, owner = known
        kind = kind or known_kind
        if name in (Config.PROFILES or {}):
            profile = _from_config(name, Config.PROFILES[name])
        else:
            profile = _base(name, kind == 'group')
        if profile.sessions == 'per_account':
            if kind == 'group':
                profile.sessions = 'per_conversation'
            else:
                profile.session_owner = owner
        if profile.per_inbox and kind != 'group':
            with _last_lock:
                profile.folder_key = _last_sender.get(conversation_id, '')
        return profile
    if kind is None and conversation_db is not None:
        try:
            kind = 'group' if conversation_db.is_group_conversation(conversation_id) else 'dm'
        except Exception:
            kind = 'dm'
    sender = None
    if kind != 'group' and conversation_db is not None:
        try:
            sender = conversation_db.last_human_sender(conversation_id)
        except Exception as e:
            logging.error(f"profiles: history lookup failed for {conversation_id}: {e}")
    return resolve_profile(conversation_id, sender, kind or 'dm')


def setting(conversation_id, key, default):
    """A per-profile number (`stack_secs`, `group_reply_secs`) for the profile this
    conversation last resolved to, else `default` (the [server] value)."""
    with _last_lock:
        known = _last.get(conversation_id)
    raw = (Config.PROFILES or {}).get(known[0]) if known else None
    value = raw.get(key) if raw else None
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)


def workdir_for(profile, conversation_id):
    """The folder Claude Code runs in for this profile and conversation (created)."""
    path = profile.workdir
    if getattr(profile, 'per_inbox', False) and path:
        # One subfolder per person. No person known (it should not happen for a DM)
        # is its own folder, never somebody's.
        safe = "".join(c for c in str(profile.folder_key) if c.isalnum())[:64] or "_nobody"
        path = os.path.join(path, safe)
    if profile.per_group_workdir:
        safe = "".join(c for c in str(conversation_id) if c.isalnum() or c in "-_")[:64] or "unknown"
        path = os.path.join(profile.workdir, f"group_{safe}")
    if path:
        os.makedirs(path, exist_ok=True)
    return path
