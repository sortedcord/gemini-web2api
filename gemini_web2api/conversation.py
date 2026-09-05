"""Native Gemini conversation state and deterministic reconciliation helpers."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .config import CONFIG


@dataclass(frozen=True)
class GeminiContinuation:
    """Opaque Gemini Web state required to continue one native conversation."""

    cid: str
    rid: str
    rcid: str
    context_token: str

    def payload_slot(self) -> list:
        return [
            self.cid,
            self.rid,
            self.rcid,
            None,
            None,
            None,
            None,
            None,
            None,
            self.context_token,
        ]


@dataclass(frozen=True)
class ConversationTurn:
    id: str
    conversation_id: str
    parent_turn_id: str | None
    response_id: str | None
    transcript_hash: str | None
    continuation: GeminiContinuation


@dataclass(frozen=True)
class ConversationResolution:
    conversation_id: str | None = None
    parent_turn: ConversationTurn | None = None
    continuation: GeminiContinuation | None = None
    matched_prefix_length: int = 0
    stateful: bool = False
    error: str | None = None


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    return value


def transcript_hash(messages: list) -> str:
    payload = json.dumps(_canonical(messages), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def account_fingerprint() -> str:
    """Return a stable deployment/account identity without storing credentials."""
    value = "{}\x00{}".format(
        CONFIG.get("conversation_account_id", "default"),
        CONFIG.get("auth_user", ""),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def namespace_from_request(request: dict, headers=None) -> str | None:
    """Resolve only explicit/trusted client namespaces, never source IPs."""
    metadata = request.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    candidates = (
        metadata.get("chat_id"),
        request.get("user"),
        metadata.get("user"),
    )
    if headers is not None:
        candidates += (
            headers.get("X-OpenWebUI-Chat-ID"),
            headers.get("X-User-ID"),
        )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return hashlib.sha256(candidate.strip().encode()).hexdigest()
    return None


def conversation_id_from_request(request: dict, headers=None) -> str | None:
    metadata = request.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("conversation_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if headers is not None:
        value = headers.get("X-Gemini-Conversation-ID")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def new_chat_requested(request: dict, headers=None) -> bool:
    metadata = request.get("metadata")
    if isinstance(metadata, dict) and metadata.get("new_conversation") is True:
        return True
    return bool(headers is not None and headers.get("X-Gemini-New-Chat", "").lower() == "true")


class ConversationStore:
    """Small SQLite-backed turn tree with process-local conversation locks."""

    def __init__(self, path: str | None = None):
        self._path = path
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._initialized = False
        self._init_guard = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(CONFIG.get("conversation_state_enabled", False))

    def initialize(self) -> None:
        if self.enabled:
            self._ensure_schema()

    @contextmanager
    def _connect(self):
        path = self._path or CONFIG.get("conversation_store_path", "/data/conversations.db")
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(path, timeout=30)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ensure_schema(self):
        if self._initialized:
            return
        with self._init_guard:
            if self._initialized:
                return
            with self._connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY,
                        namespace_hash TEXT,
                        current_head_id TEXT,
                        account_fingerprint TEXT NOT NULL,
                        system_fingerprint TEXT,
                        tools_fingerprint TEXT,
                        temporary_chat INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS conversation_turns (
                        id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        parent_turn_id TEXT,
                        response_id TEXT UNIQUE,
                        transcript_hash TEXT,
                        request_hash TEXT,
                        cid TEXT NOT NULL,
                        rid TEXT NOT NULL,
                        rcid TEXT NOT NULL,
                        context_token TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_namespace
                        ON conversations(namespace_hash);
                    CREATE INDEX IF NOT EXISTS idx_turns_transcript
                        ON conversation_turns(transcript_hash);
                    CREATE INDEX IF NOT EXISTS idx_turns_response
                        ON conversation_turns(response_id);
                    """
                )
            self._initialized = True

    def lock_for(self, conversation_id: str):
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.RLock())

    def _cleanup(self, db, now: int):
        db.execute("DELETE FROM conversation_turns WHERE expires_at <= ?", (now,))
        db.execute(
            "DELETE FROM conversations WHERE expires_at <= ? OR id NOT IN "
            "(SELECT DISTINCT conversation_id FROM conversation_turns)",
            (now,),
        )
        limit = max(1, int(CONFIG.get("conversation_max_conversations", 10000)))
        stale = db.execute(
            "SELECT id FROM conversations ORDER BY updated_at ASC LIMIT "
            "MAX(0, (SELECT COUNT(*) FROM conversations) - ?)",
            (limit,),
        ).fetchall()
        for row in stale:
            db.execute("DELETE FROM conversation_turns WHERE conversation_id=?", (row[0],))
            db.execute("DELETE FROM conversations WHERE id=?", (row[0],))

    @staticmethod
    def _turn(row) -> ConversationTurn:
        return ConversationTurn(
            id=row["id"],
            conversation_id=row["conversation_id"],
            parent_turn_id=row["parent_turn_id"],
            response_id=row["response_id"],
            transcript_hash=row["transcript_hash"],
            continuation=GeminiContinuation(
                row["cid"], row["rid"], row["rcid"], row["context_token"]
            ),
        )

    def create_conversation(self, namespace_hash: str | None) -> str:
        self._ensure_schema()
        now = int(time.time())
        conversation_id = "conv_" + secrets.token_urlsafe(32)
        ttl = max(60, int(CONFIG.get("conversation_ttl_sec", 604800)))
        with self._connect() as db:
            self._cleanup(db, now)
            db.execute(
                "INSERT INTO conversations VALUES (?, ?, NULL, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    conversation_id,
                    namespace_hash,
                    account_fingerprint(),
                    int(bool(CONFIG.get("temporary_chats", False))),
                    now,
                    now,
                    now + ttl,
                ),
            )
        return conversation_id

    def namespace_for(self, conversation_id: str) -> str | None:
        self._ensure_schema()
        with self._connect() as db:
            row = db.execute(
                "SELECT namespace_hash FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            return row[0] if row else None

    def get_current(self, conversation_id: str, namespace_hash: str | None) -> ConversationTurn | None:
        self._ensure_schema()
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT t.* FROM conversation_turns t JOIN conversations c "
                "ON c.current_head_id=t.id WHERE c.id=? AND c.expires_at>? "
                "AND c.account_fingerprint=? AND (c.namespace_hash IS ? OR c.namespace_hash=?)",
                (conversation_id, now, account_fingerprint(), namespace_hash, namespace_hash),
            ).fetchone()
            return self._turn(row) if row else None

    def get_by_response(self, response_id: str) -> ConversationTurn | None:
        self._ensure_schema()
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT t.* FROM conversation_turns t JOIN conversations c "
                "ON c.id=t.conversation_id WHERE t.response_id=? AND t.status='completed' "
                "AND t.expires_at>? AND c.account_fingerprint=?",
                (response_id, now, account_fingerprint()),
            ).fetchone()
            return self._turn(row) if row else None

    def find_by_hash(self, namespace_hash: str, digest: str) -> ConversationTurn | None:
        self._ensure_schema()
        if not namespace_hash:
            return None
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT t.* FROM conversation_turns t JOIN conversations c "
                "ON c.id=t.conversation_id WHERE c.namespace_hash=? "
                "AND t.transcript_hash=? AND t.status='completed' "
                "AND t.expires_at>? AND c.account_fingerprint=? "
                "ORDER BY t.created_at DESC LIMIT 1",
                (namespace_hash, digest, now, account_fingerprint()),
            ).fetchone()
            return self._turn(row) if row else None

    def save_turn(
        self,
        conversation_id: str,
        namespace_hash: str | None,
        parent_turn_id: str | None,
        response_id: str | None,
        digest: str | None,
        continuation: GeminiContinuation,
    ) -> ConversationTurn:
        self._ensure_schema()
        now = int(time.time())
        ttl = max(60, int(CONFIG.get("conversation_ttl_sec", 604800)))
        turn_id = "turn_" + secrets.token_urlsafe(24)
        with self._connect() as db:
            self._cleanup(db, now)
            existing = db.execute(
                "SELECT namespace_hash, account_fingerprint FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if existing and (
                existing["account_fingerprint"] != account_fingerprint()
                or existing["namespace_hash"] != namespace_hash
            ):
                raise ValueError("conversation ID belongs to another client namespace")
            db.execute(
                "INSERT OR IGNORE INTO conversations "
                "(id, namespace_hash, current_head_id, account_fingerprint, "
                "temporary_chat, created_at, updated_at, expires_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    namespace_hash,
                    account_fingerprint(),
                    int(bool(CONFIG.get("temporary_chats", False))),
                    now,
                    now,
                    now + ttl,
                ),
            )
            db.execute(
                "UPDATE conversations SET updated_at=?, expires_at=? WHERE id=?",
                (now, now + ttl, conversation_id),
            )
            db.execute(
                "INSERT INTO conversation_turns "
                "(id, conversation_id, parent_turn_id, response_id, transcript_hash, request_hash, "
                "cid, rid, rcid, context_token, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
                (
                    turn_id,
                    conversation_id,
                    parent_turn_id,
                    response_id,
                    digest,
                    digest,
                    continuation.cid,
                    continuation.rid,
                    continuation.rcid,
                    continuation.context_token,
                    now,
                    now + ttl,
                ),
            )
            current = db.execute(
                "SELECT current_head_id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()[0]
            if current is None or current == parent_turn_id:
                db.execute(
                    "UPDATE conversations SET current_head_id=?, updated_at=?, expires_at=? WHERE id=?",
                    (turn_id, now, now + ttl, conversation_id),
                )
            row = db.execute("SELECT * FROM conversation_turns WHERE id=?", (turn_id,)).fetchone()
            return self._turn(row)


STORE = ConversationStore()


def resolve_conversation(
    request: dict,
    messages: list,
    headers=None,
) -> ConversationResolution:
    """Resolve a request without semantic guessing or cross-namespace lookup."""
    if not STORE.enabled:
        return ConversationResolution()
    namespace = namespace_from_request(request, headers)
    explicit_id = conversation_id_from_request(request, headers)
    if new_chat_requested(request, headers):
        return ConversationResolution(
            conversation_id=STORE.create_conversation(namespace), stateful=True
        )

    previous_response_id = request.get("previous_response_id")
    if isinstance(previous_response_id, str) and previous_response_id:
        parent = STORE.get_by_response(previous_response_id)
        if not parent:
            return ConversationResolution(
                stateful=True,
                error=("conversation state expired or unknown for previous_response_id"),
            )
        return ConversationResolution(
            conversation_id=parent.conversation_id,
            parent_turn=parent,
            continuation=parent.continuation,
            matched_prefix_length=0,
            stateful=True,
        )

    if explicit_id:
        current = STORE.get_current(explicit_id, namespace)
        if current:
            if len(messages) <= 1:
                return ConversationResolution(
                    conversation_id=explicit_id,
                    parent_turn=current,
                    continuation=current.continuation,
                    stateful=True,
                )
            for length in range(len(messages) - 1, 0, -1):
                digest = transcript_hash(messages[:length])
                parent = (
                    STORE.find_by_hash(namespace, digest) if namespace else None
                )
                if not parent and current.transcript_hash == digest:
                    parent = current
                if parent and parent.conversation_id == explicit_id:
                    return ConversationResolution(
                        conversation_id=explicit_id,
                        parent_turn=parent,
                        continuation=parent.continuation,
                        matched_prefix_length=length,
                        stateful=True,
                    )
            return ConversationResolution(
                conversation_id=explicit_id, stateful=True
            )
        return ConversationResolution(conversation_id=explicit_id, stateful=True)

    if namespace and len(messages) > 1:
        for length in range(len(messages) - 1, 0, -1):
            parent = STORE.find_by_hash(namespace, transcript_hash(messages[:length]))
            if parent:
                return ConversationResolution(
                    conversation_id=parent.conversation_id,
                    parent_turn=parent,
                    continuation=parent.continuation,
                    matched_prefix_length=length,
                    stateful=True,
                )
    return ConversationResolution(stateful=True)
