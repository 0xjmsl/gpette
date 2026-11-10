"""
Database logger — captures stdout, stderr, and Python logging into the
server_logs table of the SQLite storage file (Config.STORAGE_PATH).

With a non-SQLite storage backend (or if the file can't be opened) nothing is
written to a database: console output and normal Python logging still work.

Usage (call once at startup, BEFORE logging.basicConfig):
    from utils.db_logger import setup_db_logging
    setup_db_logging("gpette")
"""

import sys
import os
import logging
import sqlite3
import threading
import queue
import time

from config import Config


_log_queue = queue.Queue(maxsize=10000)
_source = "gpette"
_running = False
_db_ok = False

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS server_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT DEFAULT (datetime('now', 'localtime')),
    source TEXT NOT NULL,
    level TEXT NOT NULL,
    module TEXT,
    message TEXT
)
"""


def _get_conn():
    path = Config.STORAGE_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_table():
    """Create the table; return True if the SQLite log sink is usable."""
    if str(Config.STORAGE_BACKEND or 'sqlite').strip().lower() != 'sqlite':
        return False
    try:
        conn = _get_conn()
        conn.execute(_CREATE_TABLE)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        sys.__stderr__.write(f"[db_logger] table init failed, logging to console only: {e}\n")
        return False


def _writer_loop():
    """Background thread — drains queue into DB in batches."""
    global _running
    while _running or not _log_queue.empty():
        batch = []
        deadline = time.time() + 2.0
        while len(batch) < 50:
            remaining = max(0.01, deadline - time.time())
            try:
                batch.append(_log_queue.get(timeout=remaining))
            except queue.Empty:
                break

        if not batch:
            continue

        try:
            conn = _get_conn()
            conn.executemany(
                "INSERT INTO server_logs (source, level, module, message) VALUES (?, ?, ?, ?)",
                [(src, level, module or "", (msg or "")[:8000]) for src, level, module, msg in batch],
            )
            conn.commit()
            conn.close()
        except Exception:
            pass  # drop silently — console output still works


def log_to_db(level, message, module=None):
    """Enqueue a log entry. Drops silently if queue full or no DB sink."""
    if not _db_ok:
        return
    try:
        _log_queue.put_nowait((_source, level, module or "", str(message)))
    except queue.Full:
        pass


class _StreamTee:
    """Wraps a stream to tee output to both the original and the DB queue."""

    def __init__(self, original, level):
        self._original = original
        self._level = level
        self._buffer = ""

    def write(self, text):
        self._original.write(text)
        if not text:
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                log_to_db(self._level, line.strip())

    def flush(self):
        if self._buffer.strip():
            log_to_db(self._level, self._buffer.strip())
            self._buffer = ""
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


class _DbLogHandler(logging.Handler):
    """Python logging handler — sends records to DB with proper level."""

    def emit(self, record):
        try:
            log_to_db(record.levelname, self.format(record), record.name)
        except Exception:
            pass


def setup_db_logging(source="gpette"):
    """Call once at startup BEFORE logging.basicConfig().
    Captures all stdout, stderr, and logging output into server_logs table."""
    global _source, _running, _db_ok
    _source = source

    _db_ok = _ensure_table()
    if not _db_ok:
        return  # no DB sink: leave stdout/stderr/logging untouched

    _running = True
    t = threading.Thread(target=_writer_loop, daemon=True, name="db-log-writer")
    t.start()

    # Tee stdout and stderr — must happen before basicConfig so the
    # StreamHandler it creates will write through the tee.
    sys.stdout = _StreamTee(sys.__stdout__, "STDOUT")
    sys.stderr = _StreamTee(sys.__stderr__, "STDERR")

    # Add structured handler to root logger (captures level + module name)
    handler = _DbLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
