"""Profiles: which environment answers which conversation.

A profile is declared in gpette.toml as `[profiles.<name>]` with a `members`
list (group conversation ids and/or sender inbox ids). A message is matched by
its group conversation id first, then by its sender's inbox id. Unmatched:

- a DM uses the `default` profile, built from the top-level settings;
- a group uses the `group` sandbox: its own empty folder under
  `[claude_code] group_workdir`, no tools, the group prompt.

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
                        conversation memory; groups stay per conversation)
    gate                open | password | <plug-in module>
    tools               Python tools (./tools) an API provider may call; [] = none
    max_turns, timeout_secs   Claude Code limits
    privacy             true = never reveal paths, user names, machine or config details
    inherit_user_settings   true = load the host user's Claude Code settings, CLAUDE.md and
                        memory (owner-only; default from [claude_code], never for groups)
    admin               true = /menu (model, timers, session, status) and full error text
    on_error            describe | generic (default: describe for admin, else generic)
    error_reply         generic: what people see when the model fails

Defaults are read from Config at resolve time, so runtime edits apply.
"""
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

from config import Config, ALL_CLAUDE_TOOLS
from modules import admin_menu

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
    admin: bool = False                   # /menu, and errors described in full (modules/admin_menu.py)
    on_error: str = 'generic'             # describe | generic: how a failed model run is answered
    error_reply: str = ''                 # generic: the text sent ('' = the built-in one)

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


def _base(name, is_group):
    """The top-level settings as a profile (the `default` one, or the `group` sandbox)."""
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
    if is_group and str(getattr(Config, 'CLAUDE_CODE_GROUPS', 'sandbox')).strip() == 'default':
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
    if p.sessions not in ('per_conversation', 'shared', 'per_account'):
        logging.warning(f"profile '{name}': sessions='{p.sessions}' unknown, using per_conversation")
        p.sessions = 'per_conversation'
    if 'admin' in raw:
        p.admin = bool(raw['admin'])
    p.on_error = str(raw.get('on_error') or ('describe' if p.admin else 'generic')).strip()
    if p.on_error not in ('describe', 'generic'):
        logging.warning(f"profile '{name}': on_error='{p.on_error}' unknown, using generic")
        p.on_error = 'generic'
    p.error_reply = str(raw.get('error_reply') or '')
    return admin_menu.apply_overrides(p)


def base_profile(is_group):
    """`default` or the `group` sandbox, for callers that have no sender to match."""
    return _base(GROUP if is_group else DEFAULT, is_group)


def _match(member_id):
    key = _norm(member_id)
    if not key:
        return None
    for name, raw in (Config.PROFILES or {}).items():
        if any(_norm(m) == key for m in (raw.get('members') or [])):
            return name
    return None


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


def resolve_profile(conversation_id, sender_inbox_id, kind):
    """The profile for a message: its group id first, then its sender, else
    `default` (DM) or the `group` sandbox (group)."""
    is_group = kind == 'group'
    name = (_match(conversation_id) if is_group else None) or _match(sender_inbox_id)
    if name:
        profile = _from_config(name, Config.PROFILES[name])
    else:
        profile = _base(GROUP if is_group else DEFAULT, is_group)
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
    profile, else its setter's, else the `group` sandbox."""
    name = (name or '').strip()
    if not name:
        return "it has no profile (bind it: scripts/timer.py bind)"
    if name not in (Config.PROFILES or {}) and name not in (DEFAULT, GROUP):
        return f"its profile '{name}' is not in the config"
    if is_group:
        expected = _match(conversation_id) or _match(owner_inbox) or GROUP
    else:
        if not _norm(owner_inbox):
            return "it has no inbox (a timer in a DM reports to one inbox)"
        expected = _match(owner_inbox) or DEFAULT
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
    if profile.sessions == 'per_account':
        if is_group:
            profile.sessions = 'per_conversation'
        else:
            profile.session_owner = owner_for(owner_inbox)
    # The fire's own tool results, and a timer set during the fire, see this.
    remember(conversation_id, profile.name, 'group' if is_group else 'dm',
             profile.session_owner, owner_inbox)
    return profile, None


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


def workdir_for(profile, conversation_id):
    """The folder Claude Code runs in for this profile and conversation (created)."""
    path = profile.workdir
    if profile.per_group_workdir:
        safe = "".join(c for c in str(conversation_id) if c.isalnum() or c in "-_")[:64] or "unknown"
        path = os.path.join(profile.workdir, f"group_{safe}")
    if path:
        os.makedirs(path, exist_ok=True)
    return path
