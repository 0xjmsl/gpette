"""Conversation storage.

The built-in backend is SQLite (Python's `sqlite3`) at `Config.STORAGE_PATH`.
Set `[storage] backend` to a dotted module path to plug in another backend: that
module must define a class `ConversationDB` with the same public methods, and
`from models.conversation_db import ConversationDB` then returns that class.
"""
import base64
import importlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from config import Config

# BIT columns in the original schema: returned as bool, like the SQL Server driver did.
_BOOL_COLUMNS = {'is_group', 'skip_session'}

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL UNIQUE,
        is_group INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        last_active INTEGER NOT NULL,
        reset_at INTEGER NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS authenticated_inboxes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inbox_id TEXT NOT NULL UNIQUE,
        eth_address TEXT NULL,
        first_authenticated_at INTEGER NOT NULL,
        last_authenticated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        inbox_id TEXT NOT NULL,
        permission_level INTEGER NOT NULL DEFAULT 0,
        joined_at INTEGER NOT NULL,
        UNIQUE (conversation_id, inbox_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inbox_id TEXT NOT NULL,
        authenticated_at INTEGER NOT NULL,
        day_key TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        sender_inbox_id TEXT NULL,
        role TEXT NOT NULL,
        timestamp INTEGER NOT NULL,
        content TEXT NULL,
        function_call TEXT NULL,
        tool_calls TEXT NULL,
        tool_call_id TEXT NULL,
        token_count INTEGER,
        mode TEXT NOT NULL,
        source TEXT NOT NULL,
        message_type TEXT DEFAULT 'text',
        attachment_data BLOB NULL,
        attachment_metadata TEXT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_messages_conv_ts ON messages_v2 (conversation_id, timestamp)",
    """
    CREATE TABLE IF NOT EXISTS resolved_contacts (
        inbox_id TEXT NOT NULL PRIMARY KEY,
        eth_address TEXT NULL,
        display_name TEXT NOT NULL,
        resolution_source TEXT NOT NULL,
        resolved_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timer_id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL,
        description TEXT NOT NULL,
        instructions TEXT NOT NULL,
        timer_type TEXT DEFAULT 'one_shot',
        interval_seconds INTEGER NULL,
        fire_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        last_fired_at INTEGER NULL,
        status TEXT DEFAULT 'active',
        skip_session INTEGER DEFAULT 0
    )
    """,
    # One running summary per history (modules/context_compactor.py): the summary text and
    # through_ts, the timestamp (ms) of the last message it covers.
    """
    CREATE TABLE IF NOT EXISTS conversation_summaries (
        summary_key TEXT NOT NULL PRIMARY KEY,
        summary TEXT NOT NULL,
        through_ts INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
]

# Columns added after a table first shipped: (table, column, type).
_MIGRATIONS = [
    ('messages_v2', 'tool_calls', 'TEXT NULL'),
    ('messages_v2', 'tool_call_id', 'TEXT NULL'),
    ('conversations', 'reset_at', 'INTEGER NULL'),
    ('timers', 'skip_session', 'INTEGER DEFAULT 0'),
]


class SQLiteConversationDB:
    """SQLite storage. One connection per thread (WAL mode); writes serialized by a lock."""

    _lock = threading.RLock()          # shared by every instance: one file, one writer
    _local = threading.local()
    _initialized_paths = set()

    def __init__(self):
        """Initialize the conversation database handler"""
        self.db_path = Config.STORAGE_PATH
        self.db_lock = self._lock
        self.init_db()

    # ------------------------------------------------------------------ plumbing

    def _conn(self):
        conns = getattr(self._local, 'conns', None)
        if conns is None:
            conns = self._local.conns = {}
        conn = conns.get(self.db_path)
        if conn is None:
            parent = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conns[self.db_path] = conn
        return conn

    @staticmethod
    def _row_to_dict(row):
        out = {}
        for key in row.keys():
            val = row[key]
            if key in _BOOL_COLUMNS and val is not None:
                val = bool(val)
            out[key] = val
        return out

    def _query(self, sql, params=()):
        """Run a SELECT; return a list of dicts."""
        with self._lock:
            cur = self._conn().execute(sql, params)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def _execute(self, sql, params=()):
        """Run a write; return rowcount."""
        with self._lock:
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def execute_query(self, sql, params=None):
        """Generic query, same return convention as the old SQL Server helper:
        a JSON string, a list of row dicts for a SELECT, {"message": ...} for a
        write, {"error": ...} on failure. `%s` placeholders are accepted.
        BLOB values come back as base64 strings (JSON cannot carry bytes)."""
        sql = sql.replace('%s', '?')
        try:
            with self._lock:
                conn = self._conn()
                cur = conn.execute(sql, tuple(params) if params else ())
                if cur.description:
                    rows = []
                    for r in cur.fetchall():
                        d = self._row_to_dict(r)
                        for k, v in d.items():
                            if isinstance(v, (bytes, bytearray, memoryview)):
                                d[k] = base64.b64encode(bytes(v)).decode('ascii')
                        rows.append(d)
                    conn.commit()
                    return json.dumps(rows)
                conn.commit()
                return json.dumps({"message": "Query executed successfully"})
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------ schema

    def init_db(self):
        """Create necessary database tables if they don't exist"""
        with self._lock:
            conn = self._conn()
            for stmt in _SCHEMA:
                conn.execute(stmt)
            for table, column, coltype in _MIGRATIONS:
                cols = {r['name'] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            conn.commit()

    # ------------------------------------------------------------------ auth

    def get_today_key(self):
        """Get a string key for today's date, used for rate limiting"""
        return datetime.now().strftime("%Y-%m-%d")

    def can_authenticate_today(self, inbox_id):
        """May this inbox be let in today? Someone already in always may; a new
        person only while today's count of new people is under
        `Config.GATE_NEW_PEOPLE_PER_DAY` (0 or less = no cap).

        auth_history gets one row per NEW person (see authenticate_inbox), so
        today's rows are today's new people. (The original query excluded every
        authenticated inbox from that count, which made it always 0: the cap
        never fired.)"""
        if inbox_id and self.is_inbox_authenticated(inbox_id):
            return True
        limit = int(getattr(Config, 'GATE_NEW_PEOPLE_PER_DAY', 10) or 0)
        if limit <= 0:
            return True
        query = "SELECT COUNT(*) AS count FROM auth_history WHERE day_key = ?"
        try:
            data = self._query(query, (self.get_today_key(),))
            if data:
                return data[0].get('count', 0) < limit
            return True
        except Exception as e:
            print(f"Error checking authentication limit: {e}")
            return False

    def is_inbox_authenticated(self, inbox_id):
        """Check if an inbox has been authenticated"""
        try:
            return len(self._query("SELECT 1 FROM authenticated_inboxes WHERE inbox_id = ?", (inbox_id,))) > 0
        except Exception:
            return False

    def authenticate_inbox(self, inbox_id, eth_address=None):
        """Mark an inbox as authenticated"""
        if not self.can_authenticate_today(inbox_id):
            return False

        current_time = int(time.time())
        today_key = self.get_today_key()

        if self.is_inbox_authenticated(inbox_id):
            self._execute("UPDATE authenticated_inboxes SET last_authenticated_at = ? WHERE inbox_id = ?",
                          (current_time, inbox_id))
        else:
            self._execute("""
            INSERT INTO authenticated_inboxes (inbox_id, eth_address, first_authenticated_at, last_authenticated_at)
            VALUES (?, ?, ?, ?)
            """, (inbox_id, eth_address, current_time, current_time))
            # Record in authentication history for rate limiting
            self._execute("INSERT INTO auth_history (inbox_id, authenticated_at, day_key) VALUES (?, ?, ?)",
                          (inbox_id, current_time, today_key))
        return True

    # ------------------------------------------------------------------ conversations

    def track_conversation(self, conversation_id, is_group=False):
        """Register or update a conversation.

        is_group is monotonic: once a conversation is marked as a group, it
        cannot be demoted back to 1:1 by a later default-False call. This
        closes a race where the first incoming message inserts the row with
        is_group=False before `group_members` syncs — without this, the
        subsequent track_conversation(is_group=True) call would only update
        last_active and the row would stay misclassified as 1:1, bypassing
        the group sandbox (no tools / isolated cwd) in ClaudeCodeService.
        """
        current_time = int(time.time())
        flag = 1 if is_group else 0
        self._execute("""
        INSERT INTO conversations (conversation_id, is_group, created_at, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            last_active = excluded.last_active,
            is_group = CASE WHEN excluded.is_group = 1 THEN 1 ELSE conversations.is_group END
        """, (conversation_id, flag, current_time, current_time))
        return True

    def commit_conversation(self, conversation_id, kind, members=None):
        """Idempotently commit a conversation's classification (and, for groups,
        its participant list) from the envelope data the daemon attaches to
        every incoming message. Called before the security check.

        - kind == 'group': mark is_group=1, register every member.
        - kind == 'dm':    mark is_group=0 only. DM peers are not group members,
          so they don't go in conversation_participants — the DM auth gate keys
          on the sender's inbox, not on membership.
        """
        if not conversation_id or not kind:
            return False

        is_group = (kind == 'group')
        self.track_conversation(conversation_id, is_group=is_group)

        if is_group and members:
            perm_map = {'super_admin': 2, 'admin': 1, 'member': 0}
            for member in members:
                inbox_id = member.get('inboxId')
                if not inbox_id:
                    continue
                perm_level = perm_map.get(member.get('permissionLevel', 'member'), 0)
                self.register_group_member(conversation_id, inbox_id, perm_level)

        return True

    def is_conversation_authenticated(self, conversation_id, inbox_id):
        """Check if this conversation is authenticated for this inbox"""
        if not self.is_inbox_authenticated(inbox_id):
            return False

        self.track_conversation(conversation_id)

        # Register the inbox as a participant if it isn't one yet
        self._execute("""
        INSERT OR IGNORE INTO conversation_participants (conversation_id, inbox_id, joined_at)
        VALUES (?, ?, ?)
        """, (conversation_id, inbox_id, int(time.time())))
        return True

    def register_group_member(self, conversation_id, inbox_id, permission_level=0):
        """Register a group member to a conversation"""
        current_time = int(time.time())
        self._execute("UPDATE conversations SET is_group = 1 WHERE conversation_id = ? AND is_group = 0",
                      (conversation_id,))
        self._execute("""
        INSERT INTO conversation_participants (conversation_id, inbox_id, permission_level, joined_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id, inbox_id) DO UPDATE SET permission_level = excluded.permission_level
        """, (conversation_id, inbox_id, permission_level, current_time))
        return True

    def get_conversation_participants(self, conversation_id):
        """Get all participants for a specific conversation"""
        try:
            return self._query("""
            SELECT cp.inbox_id, cp.permission_level, cp.joined_at, ai.eth_address
            FROM conversation_participants cp
            LEFT JOIN authenticated_inboxes ai ON cp.inbox_id = ai.inbox_id
            WHERE cp.conversation_id = ?
            """, (conversation_id,))
        except Exception:
            return []

    def get_resolved_contact(self, inbox_id):
        """Get a cached resolved contact if not expired"""
        try:
            data = self._query("""
            SELECT display_name, eth_address, resolution_source, resolved_at, expires_at
            FROM resolved_contacts
            WHERE inbox_id = ? AND expires_at > ?
            """, (inbox_id, int(time.time())))
            return data[0] if data else None
        except Exception:
            return None

    def store_resolved_contact(self, inbox_id, eth_address, display_name, source, ttl=86400):
        """Cache a resolved contact with TTL (default 24h)"""
        current_time = int(time.time())
        expires_at = current_time + ttl
        self._execute("""
        INSERT INTO resolved_contacts (inbox_id, eth_address, display_name, resolution_source, resolved_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(inbox_id) DO UPDATE SET
            eth_address = excluded.eth_address,
            display_name = excluded.display_name,
            resolution_source = excluded.resolution_source,
            resolved_at = excluded.resolved_at,
            expires_at = excluded.expires_at
        """, (inbox_id, eth_address, display_name, source, current_time, expires_at))

    def is_group_conversation(self, conversation_id):
        """Check if a conversation is a group conversation"""
        try:
            data = self._query("SELECT is_group FROM conversations WHERE conversation_id = ?", (conversation_id,))
            return bool(data[0].get('is_group', 0)) if data else False
        except Exception:
            return False

    def has_authenticated_participant(self, conversation_id):
        """Return True if any participant of this group conversation is in
        authenticated_inboxes. Used by the password gate to gate group messages —
        groups are allowed iff at least one known/authenticated identity is a
        member, regardless of who is currently speaking."""
        try:
            data = self._query("""
            SELECT COUNT(*) AS cnt
            FROM conversation_participants cp
            INNER JOIN authenticated_inboxes ai ON cp.inbox_id = ai.inbox_id
            WHERE cp.conversation_id = ?
            """, (conversation_id,))
            return int(data[0].get('cnt', 0)) > 0 if data else False
        except Exception:
            return False

    def get_participant_address(self, inbox_id):
        """Get the eth address for a participant from authenticated_inboxes"""
        try:
            data = self._query("""
            SELECT ai.eth_address
            FROM authenticated_inboxes ai
            WHERE ai.inbox_id = ? AND ai.eth_address IS NOT NULL
            LIMIT 1
            """, (inbox_id,))
            return data[0].get('eth_address') if data else None
        except Exception:
            return None

    # ------------------------------------------------------------------ messages

    def store_message(self, message):
        """Store a message with conversation context"""
        conversation_id = message.get('conversationId')
        sender_inbox_id = message.get('senderInboxId')

        if not conversation_id:
            return False

        self.track_conversation(conversation_id)

        attachment_data = None
        attachment_metadata = None
        message_type = 'text'
        content_to_store = message.get('content')
        content_to_store_in_db = content_to_store

        if isinstance(content_to_store, dict) and content_to_store.get('type') == 'attachment':
            message_type = 'attachment'
            content_copy = json.loads(json.dumps(content_to_store))  # Deep copy

            if 'data' in content_copy:
                # Decode the Base64 string to bytes for storage
                base64_data = content_copy.pop('data', None)
                if base64_data:
                    attachment_data = base64.b64decode(base64_data)
                # Metadata is the content dict without the actual data
                attachment_metadata = json.dumps(content_copy)
                # Store a placeholder in the 'content' column
                content_to_store_in_db = {"type": "attachment", "value": f"Attachment: {content_copy.get('filename', 'file')}"}
            else:
                attachment_metadata = json.dumps(content_copy)

        params = (
            conversation_id,
            sender_inbox_id,
            message['role'],
            message['timestamp'],
            json.dumps(content_to_store_in_db) if content_to_store_in_db else None,
            json.dumps(message.get('function_call')) if message.get('function_call') else None,
            json.dumps(message.get('tool_calls')) if message.get('tool_calls') else None,
            message.get('tool_call_id'),
            message.get("token_count"),
            message.get('mode', 'personal'),
            message.get('source', 'unknown'),
            message_type,
            attachment_data,
            attachment_metadata,
        )
        self._execute("""
        INSERT INTO messages_v2 (
            conversation_id, sender_inbox_id, role, timestamp,
            content, function_call, tool_calls, tool_call_id, token_count, mode, source,
            message_type, attachment_data, attachment_metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, params)
        return True

    def get_conversation_history(self, conversation_ids, limit=50):
        """
        Get conversation history for one or more conversation IDs.
        Accepts a single string or a list of strings (for thread joining).
        Returns the most recent `limit` messages, oldest first; attachment
        bytes come back Base64-encoded under content['data'].
        """
        if isinstance(conversation_ids, str):
            conversation_ids = [conversation_ids]
        if not conversation_ids:
            return []

        placeholders = ','.join(['?'] * len(conversation_ids))
        query = f"""
        SELECT * FROM (
            SELECT
                m.id, m.conversation_id, m.sender_inbox_id, m.role, m.timestamp,
                m.content, m.function_call, m.tool_calls, m.tool_call_id, m.token_count,
                m.mode, m.source, m.message_type, m.attachment_metadata, m.attachment_data
            FROM messages_v2 m
            LEFT JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE m.conversation_id IN ({placeholders})
              AND m.role != 'Proactive'
              AND (c.reset_at IS NULL OR m.timestamp > c.reset_at)
            ORDER BY m.timestamp DESC, m.id DESC
            LIMIT ?
        ) ORDER BY timestamp ASC, id ASC
        """

        try:
            messages = self._query(query, tuple(conversation_ids) + (limit,))
            return [self._format_message(msg) for msg in messages]
        except Exception as e:
            print(f"Error processing conversation history: {e}")
            return []

    @staticmethod
    def _format_message(msg):
        """A messages_v2 row as the dict store_message takes (plus contentType)."""
        formatted_msg = {
            'role': msg.get('role'),
            'timestamp': msg.get('timestamp'),
            'content': json.loads(msg.get('content')) if msg.get('content') else None,
            'function_call': json.loads(msg.get('function_call')) if msg.get('function_call') else None,
            'tool_calls': json.loads(msg.get('tool_calls')) if msg.get('tool_calls') else None,
            'tool_call_id': msg.get('tool_call_id'),
            'token_count': msg.get('token_count'),
            'mode': msg.get('mode'),
            'source': msg.get('source'),
            'conversationId': msg.get('conversation_id'),
            'senderInboxId': msg.get('sender_inbox_id'),
            'contentType': msg.get('message_type'),
        }

        if msg.get('message_type') == 'attachment':
            if not isinstance(formatted_msg.get('content'), dict):
                formatted_msg['content'] = {}
            formatted_msg['content']['type'] = 'attachment'

            if msg.get('attachment_metadata'):
                try:
                    formatted_msg['content'].update(json.loads(msg['attachment_metadata']))
                except Exception as e:
                    print(f"Error parsing attachment_metadata for message {msg.get('id')}: {e}")

            if msg.get('attachment_data'):
                formatted_msg['content']['data'] = base64.b64encode(bytes(msg['attachment_data'])).decode('ascii')

        return formatted_msg

    def reset_conversation(self, conversation_id):
        """Mark a conversation as reset so history before this point is excluded"""
        now = int(datetime.now().timestamp() * 1000)
        self._execute("UPDATE conversations SET reset_at = ? WHERE conversation_id = ?", (now, conversation_id))
        self.clear_summaries(conversation_id)
        return True

    # ------------------------------------------------------------- summaries

    def get_summary(self, summary_key):
        """The running summary of one history: {summary, through_ts, updated_at} or None."""
        rows = self._query("SELECT summary, through_ts, updated_at FROM conversation_summaries WHERE summary_key = ?",
                           (summary_key,))
        return rows[0] if rows else None

    def save_summary(self, summary_key, summary, through_ts):
        self._execute("""
        INSERT INTO conversation_summaries (summary_key, summary, through_ts, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(summary_key) DO UPDATE SET summary = excluded.summary, through_ts = excluded.through_ts,
                                               updated_at = excluded.updated_at
        """, (summary_key, summary, int(through_ts), int(time.time())))
        return True

    def clear_summaries(self, conversation_id):
        """Drop every summary whose history includes this conversation (a reset starts clean)."""
        self._execute("DELETE FROM conversation_summaries WHERE summary_key = ? OR summary_key LIKE ? "
                      "OR summary_key LIKE ? OR summary_key LIKE ?",
                      (conversation_id, conversation_id + '|%', '%|' + conversation_id, '%|' + conversation_id + '|%'))
        return True

    def get_authenticated_inboxes(self):
        """Get all authenticated inboxes"""
        try:
            return self._query("SELECT * FROM authenticated_inboxes")
        except Exception:
            return []

    def get_active_conversations(self, hours=24):
        """Get conversations active in the last X hours"""
        cutoff_time = int(time.time()) - (hours * 3600)
        try:
            return self._query("SELECT * FROM conversations WHERE last_active > ?", (cutoff_time,))
        except Exception:
            return []

    # ==================== EXPORT / IMPORT (scripts/history.py) ====================
    # Whole-store reads and as-is writes, so history can move between backends.
    # A plug-in backend needs these too for `scripts/history.py` to work on it.

    def list_conversations(self):
        """Every conversation: conversation_id, is_group, created_at, last_active, reset_at."""
        return self._query("""
        SELECT conversation_id, is_group, created_at, last_active, reset_at
        FROM conversations ORDER BY conversation_id
        """)

    def list_messages(self, conversation_id):
        """Every message of one conversation, oldest first, in the shape
        get_conversation_history returns. No limit, no reset cut-off, and
        'Proactive' messages included."""
        rows = self._query("""
        SELECT id, conversation_id, sender_inbox_id, role, timestamp, content, function_call,
               tool_calls, tool_call_id, token_count, mode, source, message_type,
               attachment_metadata, attachment_data
        FROM messages_v2 WHERE conversation_id = ?
        ORDER BY timestamp ASC, id ASC
        """, (conversation_id,))
        return [self._format_message(r) for r in rows]

    def list_timers(self):
        """Every timer, any status, with skip_session."""
        return self._query(f"SELECT {self._TIMER_COLS}, skip_session FROM timers ORDER BY timer_id")

    def restore_conversation(self, conversation_id, is_group, created_at, last_active, reset_at=None):
        """Create or overwrite a conversation row with the given values."""
        self._execute("""
        INSERT INTO conversations (conversation_id, is_group, created_at, last_active, reset_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            is_group = excluded.is_group, created_at = excluded.created_at,
            last_active = excluded.last_active, reset_at = excluded.reset_at
        """, (conversation_id, 1 if is_group else 0, created_at, last_active, reset_at))
        return True

    def restore_participant(self, conversation_id, inbox_id, permission_level=0, joined_at=None):
        """Create or overwrite a participant. Unlike register_group_member, it
        leaves the conversation's is_group alone (DMs have participants too)."""
        self._execute("""
        INSERT INTO conversation_participants (conversation_id, inbox_id, permission_level, joined_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id, inbox_id) DO UPDATE SET
            permission_level = excluded.permission_level, joined_at = excluded.joined_at
        """, (conversation_id, inbox_id, permission_level,
              joined_at if joined_at is not None else int(time.time())))
        return True

    def restore_authenticated_inbox(self, inbox_id, eth_address, first_authenticated_at, last_authenticated_at):
        """Create or overwrite an allowed inbox. Unlike authenticate_inbox, it
        skips the daily new-people cap and writes no auth_history row."""
        self._execute("""
        INSERT INTO authenticated_inboxes (inbox_id, eth_address, first_authenticated_at, last_authenticated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(inbox_id) DO UPDATE SET
            eth_address = excluded.eth_address,
            first_authenticated_at = excluded.first_authenticated_at,
            last_authenticated_at = excluded.last_authenticated_at
        """, (inbox_id, eth_address, first_authenticated_at, last_authenticated_at))
        return True

    def restore_timer(self, timer_id, conversation_id, description, instructions, fire_at, created_at,
                      timer_type='one_shot', interval_seconds=None, last_fired_at=None,
                      status='active', skip_session=False):
        """Create or overwrite a timer with the given id and status."""
        self._execute("""
        INSERT INTO timers (timer_id, conversation_id, description, instructions, timer_type,
                            interval_seconds, fire_at, created_at, last_fired_at, status, skip_session)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(timer_id) DO UPDATE SET
            conversation_id = excluded.conversation_id, description = excluded.description,
            instructions = excluded.instructions, timer_type = excluded.timer_type,
            interval_seconds = excluded.interval_seconds, fire_at = excluded.fire_at,
            created_at = excluded.created_at, last_fired_at = excluded.last_fired_at,
            status = excluded.status, skip_session = excluded.skip_session
        """, (timer_id, conversation_id, description, instructions, timer_type, interval_seconds,
              fire_at, created_at, last_fired_at, status, 1 if skip_session else 0))
        return True

    # ==================== TIMER METHODS ====================

    _TIMER_COLS = "timer_id, conversation_id, description, instructions, timer_type, interval_seconds, fire_at, created_at, last_fired_at, status"

    def create_timer(self, conversation_id, description, instructions, fire_at, timer_type='one_shot',
                     interval_seconds=None, skip_session=False):
        """Create a new timer and return its timer_id. skip_session: the fire
        runs in a fresh session, without the chat's history."""
        timer_id = uuid.uuid4().hex[:8]
        created_at = int(time.time())
        self._execute("""
        INSERT INTO timers (timer_id, conversation_id, description, instructions, timer_type, interval_seconds, fire_at, created_at, status, skip_session)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (timer_id, conversation_id, description, instructions, timer_type, interval_seconds, fire_at,
              created_at, 1 if skip_session else 0))
        return timer_id

    def last_human_sender(self, conversation_id):
        """The inbox of the newest message a person sent in this conversation, or
        None. A timer fire carries no sender; this is how it finds whose profile
        to run in after a restart. It reads the whole conversation on purpose
        (not the recent window, and not only what follows a reset): a chat that
        is mostly timer fires can go hundreds of rows without a human message."""
        try:
            rows = self._query("""
            SELECT sender_inbox_id FROM messages_v2
            WHERE conversation_id = ? AND role = 'user'
              AND sender_inbox_id IS NOT NULL AND sender_inbox_id != ''
            ORDER BY timestamp DESC, id DESC LIMIT 1
            """, (conversation_id,))
            return rows[0]['sender_inbox_id'] if rows else None
        except Exception as e:
            logging.error(f"last_human_sender failed for {conversation_id}: {e}")
            return None

    def get_timers_for_conversation(self, conversation_id, status='active'):
        """Get all timers for a conversation filtered by status"""
        try:
            return self._query(f"""
            SELECT {self._TIMER_COLS}
            FROM timers
            WHERE conversation_id = ? AND status = ?
            ORDER BY fire_at ASC
            """, (conversation_id, status))
        except Exception:
            return []

    def get_timer_by_id(self, timer_id):
        """Get a single timer by its timer_id"""
        try:
            data = self._query(f"SELECT {self._TIMER_COLS} FROM timers WHERE timer_id = ?", (timer_id,))
            return data[0] if data else None
        except Exception:
            return None

    def update_timer(self, timer_id, **kwargs):
        """Update timer fields (description, instructions, fire_at, timer_type, interval_seconds, skip_session)"""
        allowed = {'description', 'instructions', 'fire_at', 'timer_type', 'interval_seconds', 'skip_session'}
        updates = []
        params = []
        for key, value in kwargs.items():
            if key in allowed:
                updates.append(f"{key} = ?")
                params.append(value)
        if not updates:
            return False
        params.append(timer_id)
        self._execute(f"UPDATE timers SET {', '.join(updates)} WHERE timer_id = ? AND status = 'active'",
                      tuple(params))
        return True

    def cancel_timer(self, timer_id):
        """Cancel a timer by setting its status to 'cancelled'"""
        self._execute("UPDATE timers SET status = 'cancelled' WHERE timer_id = ? AND status = 'active'", (timer_id,))
        return True

    def get_due_timers(self):
        """Get all active timers whose fire_at has passed"""
        try:
            return self._query(f"""
            SELECT {self._TIMER_COLS}, skip_session
            FROM timers
            WHERE status = 'active' AND fire_at <= ?
            """, (int(time.time()),))
        except Exception:
            return []

    def mark_timer_fired(self, timer_id):
        """Mark a one-shot timer as fired"""
        self._execute("UPDATE timers SET status = 'fired', last_fired_at = ? WHERE timer_id = ?",
                      (int(time.time()), timer_id))

    def advance_recurring_timer(self, timer_id, interval_seconds):
        """Advance a recurring timer's fire_at by its interval"""
        self._execute("UPDATE timers SET fire_at = fire_at + ?, last_fired_at = ? WHERE timer_id = ?",
                      (interval_seconds, int(time.time()), timer_id))


def _resolve_backend():
    backend = (Config.STORAGE_BACKEND or 'sqlite').strip()
    if backend.lower() == 'sqlite':
        return SQLiteConversationDB
    module = importlib.import_module(backend)
    return getattr(module, 'ConversationDB')


ConversationDB = _resolve_backend()
