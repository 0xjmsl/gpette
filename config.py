"""gpette configuration.

Every setting lives in ONE file, `gpette.toml` (copy `gpette.example.toml` to start).
Secrets (API keys) are never in that file: they come from environment variables.
Point at another file with `GPETTE_CONFIG=/path/to/file.toml`.

The `Config` class below is what the code reads. Its attribute names are stable;
the TOML sections map onto them in `_apply()`.
"""
import json
import os
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.getenv('GPETTE_CONFIG', os.path.join(HERE, 'gpette.toml'))


def _load_toml(path):
    try:
        with open(path, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _get(cfg, dotted, default):
    node = cfg
    for part in dotted.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _abs(path):
    """Paths in the config are relative to the config file's folder."""
    if not path:
        return path
    base = os.path.dirname(os.path.abspath(CONFIG_PATH))
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


# Every standard Claude Code tool name: the fence for profiles with no tools.
ALL_CLAUDE_TOOLS = (
    'Bash,BashOutput,KillBash,Read,Edit,Write,Glob,Grep,NotebookEdit,'
    'WebFetch,WebSearch,Task,TodoWrite,ExitPlanMode,SlashCommand,'
    'PowerShell,Agent,Skill,ToolSearch,Workflow,Monitor,CronCreate,CronDelete,CronList,'
    'ScheduleWakeup,SendMessage,ListAgents,TaskStop,LSP,RemoteTrigger,PushNotification,'
    'EnterWorktree,ExitWorktree,mcp__*'
)


class Config:
    # --- connection to xmtp_daemon -------------------------------------------------
    XMTP_DAEMON_URL = 'http://127.0.0.1:3100'

    # --- this server -----------------------------------------------------------------
    API_HOST = '127.0.0.1'                     # keep local: the web UI and /api/config are not authenticated
    API_PORT = 4849
    DEBUG = False
    LOG_LEVEL = 'INFO'
    DATA_DIR = os.path.join(HERE, 'data')
    LOG_FILE = os.path.join(DATA_DIR, 'logs', 'app.log')
    UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
    PROCESSED_FOLDER = os.path.join(DATA_DIR, 'processed')
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024
    QUEUE_MAX_SIZE = 1000
    SOCKETIO_PING_TIMEOUT = 60
    SOCKETIO_PING_INTERVAL = 25
    SOCKETIO_UPDATE_INTERVAL = 20
    SOCKETIO_ERROR_RETRY_INTERVAL = 20
    SERVICE_ERROR_RETRY_INTERVAL = 20
    SERVICE_UPDATE_INTERVAL = 10
    SOCKETIO_MAX_HTTP_BUFFER = 10 * 1024 * 1024
    SERVER_RESTART_INTERVAL = 3600
    SEND_READ_RECEIPTS = True
    PROACTIVE_CHECK_INTERVAL = 60             # how often due timers are checked (s)

    # --- storage ---------------------------------------------------------------------
    STORAGE_BACKEND = 'sqlite'                 # 'sqlite' (built in) or a plug-in name
    STORAGE_PATH = os.path.join(DATA_DIR, 'gpette.db')

    # --- default LLM -----------------------------------------------------------------
    LLM_SERVICE = 'claude-code'                # claude-code | claude | openai | gemini | lmstudio | ollama
    LLM_MODELS = {'gemini': 'gemini-flash-lite-latest', 'claude': 'claude-sonnet-4-5',
                  'openai': 'gpt-4.1', 'ollama': 'qwen3', 'lmstudio': None}
    LLM_MAX_TOKENS = 8192
    LLM_TEMPERATURE = 0.7
    LLM_REQUEST_TIMEOUT = 120
    OLLAMA_HOST = 'http://localhost:11434'
    LM_STUDIO_HOST = 'http://localhost:1234/v1'
    SYSTEM_PROMPT_FILE = ''                    # API providers' system prompt (a text file); empty = built-in

    # --- Claude Code (the default profile's values) ---------------------------------
    CLAUDE_CODE_CWD = os.path.join(HERE, 'workdir')
    CLAUDE_CODE_MODEL = ''
    CLAUDE_CODE_ALLOWED_TOOLS = 'Read,Glob,Grep'
    CLAUDE_CODE_MAX_TURNS = 50
    CLAUDE_CODE_TIMEOUT = 3600
    PRIVACY = True                              # bots never reveal paths, user names, machine or config details
    CLAUDE_CODE_INHERIT_USER_SETTINGS = False   # True = also load the host user's CLAUDE.md, memory and permissions
    CLAUDE_CODE_FENCED_TIMEOUT = 180            # cap for no-tools runs (they can stall)
    CLAUDE_CODE_TRANSCRIPTS = False             # write conversations/<owner>/ files the bot can read
    CLAUDE_CODE_GROUPS = 'sandbox'              # groups in no profile: sandbox (empty, no tools) | default
    CLAUDE_CODE_SYSTEM_PROMPT = (
        "You are running as a gpette chat bot. Messages arrive over XMTP chat, not a terminal. "
        "Respond conversationally. Timer fires are automated system events: act on the instructions naturally.")
    CLAUDE_CODE_GROUP_CWD = os.path.join(HERE, 'groups')
    CLAUDE_CODE_GROUP_DISALLOWED_TOOLS = ALL_CLAUDE_TOOLS
    CLAUDE_CODE_GROUP_SYSTEM_PROMPT = (
        "You are a gpette chat bot in a group XMTP chat. Respond casually. You have no tools: "
        "you cannot read files, run commands, search the web or execute anything. Just talk.")

    # --- sessions ----------------------------------------------------------------------
    SESSION_ROUTING = 'per_conversation'       # per_conversation | shared | per_account
    SESSION_OWNER_RESOLVER = ''                # per_account: module with owner_for(inbox_id) -> str | None
    THREAD_JOINS = []                          # [["conv_a", "conv_b"], ...] share history
    THREAD_JOINS_ENABLED = True

    # --- access gate -------------------------------------------------------------------
    GATE_KIND = 'open'                         # open | password | <plug-in module path>
    GATE_PASSWORD = 'password'
    GATE_WELCOME = 'You are in. Say hello.'    # reply to the right password
    GATE_LIMIT_REPLY = 'No more new people today. Please try again tomorrow.'
    GATE_NEW_PEOPLE_PER_DAY = 10
    GATE_MESSAGES_PER_PERSON_PER_DAY = 0       # 0 = no cap

    # --- profiles (filled from [profiles.*]) -------------------------------------------
    PROFILES = {}

    # --- plug-in settings (filled from [plugins.*]; see plugin_settings) ----------------
    PLUGINS = {}

    # --- who this gpette answers (filled from [filter]; see modules/filter.py) -----------
    FILTER = {}

    # --- context compaction -----------------------------------------------------------
    # API models only (Claude Code keeps its own session). See modules/context_compactor.py.
    COMPACTION_PROVIDER = 'same'               # same | lm_studio | claude | openai | gemini | ollama | disabled
    COMPACTION_MODEL = ''                      # with a named provider: its model; empty = its cheapest default
    COMPACTION_AFTER = 30                      # fold when more than this many messages follow the summary
    COMPACTION_KEEP = 10                       # the latest messages a fold leaves word for word
    COMPACTION_SUMMARY_TOKENS = 600

    # --- semantic memory (optional) ---------------------------------------------------
    MEMORY_ENABLED = False
    MEMORY_EMBEDDING_PROVIDER = 'lmstudio'
    MEMORY_EMBEDDING_MODEL = 'text-embedding-nomic-embed-text-v1.5'
    MEMORY_MAX_RESULTS = 5
    MEMORY_MIN_SCORE = 0.35
    MEMORY_CHUNK_TOKENS = 400
    MEMORY_INDEX_DEBOUNCE = 5

    # --- secrets: environment only -----------------------------------------------------
    ANTROKEY = os.getenv('ANTHROPIC_API_KEY', '') or os.getenv('ANTHROPICS_API_KEY', '')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

    # --- sender names (optional plug-in) -----------------------------------------------
    # A dotted module path whose `resolve(inbox_id, address=None)` returns a display
    # name or None. Empty = no resolution (senders show as a short inbox id).
    SENDER_RESOLVER = ''

    @staticmethod
    def init_app(app):
        pass


# TOML key -> Config attribute (+ whether it is a path to resolve).
_MAP = {
    'daemon.url': ('XMTP_DAEMON_URL', False),
    'server.host': ('API_HOST', False),
    'server.port': ('API_PORT', False),
    'server.debug': ('DEBUG', False),
    'server.log_level': ('LOG_LEVEL', False),
    'server.data_dir': ('DATA_DIR', True),
    'server.read_receipts': ('SEND_READ_RECEIPTS', False),
    'server.timer_check_secs': ('PROACTIVE_CHECK_INTERVAL', False),
    'storage.backend': ('STORAGE_BACKEND', False),
    'storage.path': ('STORAGE_PATH', True),
    'llm.provider': ('LLM_SERVICE', False),
    'llm.max_tokens': ('LLM_MAX_TOKENS', False),
    'llm.temperature': ('LLM_TEMPERATURE', False),
    'llm.request_timeout': ('LLM_REQUEST_TIMEOUT', False),
    'llm.ollama_host': ('OLLAMA_HOST', False),
    'llm.lmstudio_host': ('LM_STUDIO_HOST', False),
    'llm.system_prompt_file': ('SYSTEM_PROMPT_FILE', True),
    'claude_code.workdir': ('CLAUDE_CODE_CWD', True),
    'claude_code.model': ('CLAUDE_CODE_MODEL', False),
    'claude_code.allowed_tools': ('CLAUDE_CODE_ALLOWED_TOOLS', False),
    'claude_code.max_turns': ('CLAUDE_CODE_MAX_TURNS', False),
    'claude_code.timeout_secs': ('CLAUDE_CODE_TIMEOUT', False),
    'claude_code.fenced_timeout_secs': ('CLAUDE_CODE_FENCED_TIMEOUT', False),
    'claude_code.transcripts': ('CLAUDE_CODE_TRANSCRIPTS', False),
    'claude_code.groups': ('CLAUDE_CODE_GROUPS', False),
    'claude_code.privacy': ('PRIVACY', False),
    'claude_code.inherit_user_settings': ('CLAUDE_CODE_INHERIT_USER_SETTINGS', False),
    'claude_code.system_prompt': ('CLAUDE_CODE_SYSTEM_PROMPT', False),
    'claude_code.group_workdir': ('CLAUDE_CODE_GROUP_CWD', True),
    'claude_code.group_system_prompt': ('CLAUDE_CODE_GROUP_SYSTEM_PROMPT', False),
    'sessions.routing': ('SESSION_ROUTING', False),
    'sessions.thread_joins': ('THREAD_JOINS', False),
    'sessions.owner_resolver': ('SESSION_OWNER_RESOLVER', False),
    'senders.resolver': ('SENDER_RESOLVER', False),
    'gate.kind': ('GATE_KIND', False),
    'gate.password': ('GATE_PASSWORD', False),
    'gate.welcome': ('GATE_WELCOME', False),
    'gate.limit_reply': ('GATE_LIMIT_REPLY', False),
    'gate.new_people_per_day': ('GATE_NEW_PEOPLE_PER_DAY', False),
    'gate.messages_per_person_per_day': ('GATE_MESSAGES_PER_PERSON_PER_DAY', False),
    'compaction.provider': ('COMPACTION_PROVIDER', False),
    'compaction.model': ('COMPACTION_MODEL', False),
    'compaction.after': ('COMPACTION_AFTER', False),
    'compaction.keep': ('COMPACTION_KEEP', False),
    'compaction.summary_tokens': ('COMPACTION_SUMMARY_TOKENS', False),
    'memory.enabled': ('MEMORY_ENABLED', False),
    'memory.embedding_provider': ('MEMORY_EMBEDDING_PROVIDER', False),
    'memory.embedding_model': ('MEMORY_EMBEDDING_MODEL', False),
}


def _apply(cfg):
    for dotted, (attr, is_path) in _MAP.items():
        val = _get(cfg, dotted, None)
        if val is not None:
            setattr(Config, attr, _abs(val) if is_path else val)
    models = _get(cfg, 'llm.models', None)
    if isinstance(models, dict):
        Config.LLM_MODELS = {**Config.LLM_MODELS, **models}
    tools = Config.CLAUDE_CODE_ALLOWED_TOOLS
    if isinstance(tools, list):
        Config.CLAUDE_CODE_ALLOWED_TOOLS = ','.join(tools)
    # Paths that follow DATA_DIR unless set explicitly.
    if _get(cfg, 'storage.path', None) is None:
        Config.STORAGE_PATH = os.path.join(Config.DATA_DIR, 'gpette.db')
    Config.LOG_FILE = os.path.join(Config.DATA_DIR, 'logs', 'app.log')
    Config.UPLOAD_FOLDER = os.path.join(Config.DATA_DIR, 'uploads')
    Config.PROCESSED_FOLDER = os.path.join(Config.DATA_DIR, 'processed')
    profiles = _get(cfg, 'profiles', {}) or {}
    Config.PROFILES = {}
    for name, p in profiles.items():
        p = dict(p)
        for key in ('workdir', 'system_prompt_file'):
            if p.get(key):
                p[key] = _abs(p[key])
        Config.PROFILES[name] = p
    Config.PLUGINS = {name: dict(v) for name, v in (_get(cfg, 'plugins', {}) or {}).items()
                      if isinstance(v, dict)}
    Config.FILTER = dict(_get(cfg, 'filter', {}) or {})


def plugin_settings(name):
    """A plug-in's own table, `[plugins.<name>]` in gpette.toml ({} if absent).

    `name` is the table name the plug-in documents (usually the last part of its
    module path). Values are returned as written; resolve paths with config_path().
    """
    return dict(Config.PLUGINS.get(name, {}))


def config_path(path):
    """A path from gpette.toml, made absolute relative to the config file."""
    return _abs(path)


_apply(_load_toml(CONFIG_PATH))
os.makedirs(os.path.dirname(Config.LOG_FILE), exist_ok=True)


def _overrides_path():
    """Runtime edits (web UI / POST /api/config) live in the data folder."""
    return os.path.join(Config.DATA_DIR, 'config_overrides.json')


def save_overrides(keys: list):
    """Persist runtime edits (web UI / POST /api/config)."""
    existing = _load_overrides()
    for key in keys:
        existing[key] = getattr(Config, key, None)
    with open(_overrides_path(), 'w') as f:
        json.dump(existing, f, indent=2)


def _load_overrides():
    try:
        with open(_overrides_path(), 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Keys editable at runtime (POST /api/config and the web UI) and how to cast them.
_b = lambda v: str(v).lower() in ('true', '1', 't')
OVERRIDE_TYPES = {
    'LLM_SERVICE': str, 'LLM_TEMPERATURE': float, 'LLM_MAX_TOKENS': int, 'LLM_REQUEST_TIMEOUT': int,
    'OLLAMA_HOST': str, 'LM_STUDIO_HOST': str,
    'COMPACTION_PROVIDER': str, 'COMPACTION_MODEL': str,
    'COMPACTION_AFTER': int, 'COMPACTION_KEEP': int, 'COMPACTION_SUMMARY_TOKENS': int,
    'PROACTIVE_CHECK_INTERVAL': int, 'SEND_READ_RECEIPTS': _b,
    'MEMORY_ENABLED': _b, 'MEMORY_EMBEDDING_PROVIDER': str, 'MEMORY_EMBEDDING_MODEL': str,
    'MEMORY_MAX_RESULTS': int, 'MEMORY_MIN_SCORE': float, 'MEMORY_CHUNK_TOKENS': int, 'MEMORY_INDEX_DEBOUNCE': int,
    'THREAD_JOINS_ENABLED': _b, 'THREAD_JOINS': lambda v: v if isinstance(v, list) else json.loads(v),
    'CLAUDE_CODE_CWD': str, 'CLAUDE_CODE_MODEL': str, 'CLAUDE_CODE_ALLOWED_TOOLS': str,
    'CLAUDE_CODE_MAX_TURNS': int, 'CLAUDE_CODE_TIMEOUT': int, 'CLAUDE_CODE_SYSTEM_PROMPT': str,
    'CLAUDE_CODE_GROUP_CWD': str, 'CLAUDE_CODE_GROUP_DISALLOWED_TOOLS': str, 'CLAUDE_CODE_GROUP_SYSTEM_PROMPT': str,
    'SESSION_ROUTING': str, 'GATE_KIND': str, 'GATE_PASSWORD': str,
    'GATE_WELCOME': str, 'GATE_LIMIT_REPLY': str,
    'GATE_NEW_PEOPLE_PER_DAY': int, 'GATE_MESSAGES_PER_PERSON_PER_DAY': int,
}
for _key, _val in _load_overrides().items():
    if hasattr(Config, _key):
        try:
            setattr(Config, _key, OVERRIDE_TYPES.get(_key, lambda v: v)(_val))
        except (ValueError, TypeError):
            pass


def validate_compaction_config():
    """Refuse to start with a broken compaction setup instead of failing later."""
    valid = {'same', 'lm_studio', 'claude', 'openai', 'gemini', 'ollama', 'disabled'}
    p = Config.COMPACTION_PROVIDER
    if p not in valid:
        raise ValueError(f"compaction.provider='{p}' is not valid. Must be one of: {sorted(valid)}")
    if not (1 <= Config.COMPACTION_KEEP < Config.COMPACTION_AFTER):
        raise ValueError("compaction: keep must be at least 1 and less than after")
    if p == 'claude' and not Config.ANTROKEY:
        raise ValueError("compaction.provider=claude needs ANTHROPIC_API_KEY")
    if p == 'openai' and not Config.OPENAI_API_KEY:
        raise ValueError("compaction.provider=openai needs OPENAI_API_KEY")
    if p == 'gemini' and not os.environ.get("GEMINI_API_KEY"):
        raise ValueError("compaction.provider=gemini needs GEMINI_API_KEY")
