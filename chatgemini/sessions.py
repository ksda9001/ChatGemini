"""SQLite cache for ordinary Gemini Web conversation metadata."""
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager


def _clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _history_hash(model: str, messages: list) -> str:
    payload = json.dumps(
        {"model": model or "", "messages": messages or []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ConversationStore:
    def __init__(self, path: str, ttl_sec: int = 86400, max_rows: int = 2000):
        self.path = path
        self.ttl_sec = max(1, int(ttl_sec or 86400))
        self.max_rows = max(1, int(max_rows or 2000))
        self._lock = threading.Lock()
        self._ready = False

    @contextmanager
    def _connection(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self._connection() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS conversation_sessions ("
                    "history_hash TEXT PRIMARY KEY, updated_at INTEGER NOT NULL, "
                    "model TEXT NOT NULL, upstream_json TEXT NOT NULL, messages_json TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversation_sessions_updated "
                    "ON conversation_sessions(updated_at)"
                )
            self._ready = True

    def _prune(self, connection, now: int):
        connection.execute(
            "DELETE FROM conversation_sessions WHERE updated_at < ?",
            (now - self.ttl_sec,),
        )
        connection.execute(
            "DELETE FROM conversation_sessions WHERE history_hash NOT IN ("
            "SELECT history_hash FROM conversation_sessions ORDER BY updated_at DESC LIMIT ?)",
            (self.max_rows,),
        )

    def save(self, model: str, upstream_state: dict, messages: list):
        if not upstream_state or not messages:
            return
        self._init()
        now = int(time.time())
        encoded_messages = json.dumps(messages, ensure_ascii=False)
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO conversation_sessions "
                    "(history_hash, updated_at, model, upstream_json, messages_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        _history_hash(model, messages),
                        now,
                        model or "",
                        json.dumps(upstream_state, ensure_ascii=False),
                        encoded_messages,
                    ),
                )
                self._prune(connection, now)

    def find(self, model: str, messages: list) -> dict:
        if len(messages or []) < 2:
            return {}
        self._init()
        # A saved turn is an exact prefix of the next OpenAI request.
        for end in range(len(messages) - 1, 0, -1):
            prefix = messages[:end]
            key = _history_hash(model, prefix)
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT upstream_json, messages_json FROM conversation_sessions "
                    "WHERE history_hash = ? AND model = ?",
                    (key, model or ""),
                ).fetchone()
            if not row:
                continue
            known = json.loads(row[1])
            if known != prefix:
                continue
            return {
                "upstream_state": json.loads(row[0]),
                "known_messages": known,
                "delta_messages": _clone(messages[end:]),
            }
        return {}
