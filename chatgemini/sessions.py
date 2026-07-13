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


def _identity_messages(messages: list) -> list:
    """Normalize only client-side presentation noise used for cache identity.

    OpenWebUI trims terminal whitespace from assistant Markdown before it sends
    the next request. Gemini may emit one or more final newlines, so treating
    those bytes as conversation identity makes an otherwise identical history
    miss its saved upstream Web session. User messages remain byte-for-byte
    intact: only terminal whitespace in assistant output is presentation noise.
    """
    normalized = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        item = _clone(message)
        if item.get("role") == "assistant" and isinstance(item.get("content"), str):
            item["content"] = item["content"].rstrip()
        normalized.append(item)
    return normalized


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
        messages = _identity_messages(messages)
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
        original_messages = _clone(messages)
        messages = _identity_messages(messages)
        # A saved turn is an exact prefix of the next OpenAI request. Assistant
        # terminal whitespace has already been normalized, while user content
        # remains exact.
        with self._connection() as connection:
            for end in range(len(messages) - 1, 0, -1):
                prefix = messages[:end]
                key = _history_hash(model, prefix)
                row = connection.execute(
                    "SELECT upstream_json, messages_json FROM conversation_sessions "
                    "WHERE history_hash = ? AND model = ?",
                    (key, model or ""),
                ).fetchone()
                if not row:
                    continue
                known = _identity_messages(json.loads(row[1]))
                if known != prefix:
                    continue
                return {
                    "upstream_state": json.loads(row[0]),
                    "known_messages": known,
                    "delta_messages": _clone(original_messages[end:]),
                }

            # Previous releases keyed records before terminal assistant
            # whitespace was normalized. Scan bounded, same-model records once
            # so existing live sessions immediately benefit from the fix.
            rows = connection.execute(
                "SELECT upstream_json, messages_json FROM conversation_sessions "
                "WHERE model = ? ORDER BY updated_at DESC LIMIT ?",
                (model or "", self.max_rows),
            ).fetchall()
            for end in range(len(messages) - 1, 0, -1):
                prefix = messages[:end]
                for upstream_json, messages_json in rows:
                    known = _identity_messages(json.loads(messages_json))
                    if known != prefix:
                        continue
                    return {
                        "upstream_state": json.loads(upstream_json),
                        "known_messages": known,
                        "delta_messages": _clone(original_messages[end:]),
                    }
        return {}
