"""Opt-in bounded persistence for recent companion dialogue context.

Transcript persistence is disabled unless the caller explicitly opts in. This store
keeps only a bounded recent window, never stores audio, and exposes a clear operation.
Optional authenticated encryption protects transcript content, but key management, backups,
long-term memory databases and host access controls remain application responsibilities.
"""
from __future__ import annotations

import json
import math
import os
import hashlib
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Protocol


class SessionStoreError(RuntimeError):
    pass


class TranscriptCipher(Protocol):
    """Host-provided authenticated encryption for transcript content at rest."""
    key_id: str
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


class FernetTranscriptCipher:
    """Optional Fernet encryption adapter; the key must be supplied by the host.

    Do not write keys to the database or source tree. Store keys in an OS secret store
    or inject them through a protected runtime secret mechanism. Requires the optional
    `cryptography` dependency (`pip install 'continuity-engine[crypto]'`).
    """
    def __init__(self, key: str | bytes):
        raw = key.encode("ascii") if isinstance(key, str) else bytes(key)
        try:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(raw)
        except ImportError as exc:
            raise RuntimeError("Install optional crypto support: pip install 'continuity-engine[crypto]'") from exc
        except Exception as exc:
            raise ValueError("invalid Fernet key") from exc
        self.key_id = hashlib.sha256(raw).hexdigest()[:16]

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be text")
        return "fernet:v1:" + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str) or not ciphertext.startswith("fernet:v1:"):
            raise SessionStoreError("transcript_ciphertext_format_invalid")
        try:
            return self._fernet.decrypt(ciphertext[len("fernet:v1:"):].encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise SessionStoreError("transcript_decryption_failed") from exc


class SQLiteConversationStore:
    """Persist recent turns after explicit opt-in.

    `enabled=True` is deliberately required to store conversation context. Without an
    injected cipher, text is stored plaintext. Retention and per-session row limits are
    enforced transactionally on each write.
    """

    def __init__(self, path: str | Path, *, enabled: bool = False,
                 max_messages_per_session: int = 64, retention_days: float = 7.0,
                 clock=time.time, cipher: TranscriptCipher | None = None,
                 restrict_file_permissions: bool = True):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be bool")
        if isinstance(max_messages_per_session, bool) or not isinstance(max_messages_per_session, int) or not 4 <= max_messages_per_session <= 1000:
            raise ValueError("max_messages_per_session must be in [4,1000]")
        if isinstance(retention_days, bool) or not isinstance(retention_days, (int, float)) or not math.isfinite(float(retention_days)) or not 0 < retention_days <= 365:
            raise ValueError("retention_days must be finite and in (0,365]")
        if cipher is not None and not (callable(getattr(cipher, "encrypt", None)) and
                                       callable(getattr(cipher, "decrypt", None)) and
                                       isinstance(getattr(cipher, "key_id", None), str) and getattr(cipher, "key_id")):
            raise ValueError("cipher must provide encrypt(), decrypt(), and a stable key_id")
        if not isinstance(restrict_file_permissions, bool):
            raise ValueError("restrict_file_permissions must be bool")
        self.path = str(path)
        self.enabled = enabled
        self.cipher = cipher
        self.restrict_file_permissions = restrict_file_permissions
        self._store_mode = "plaintext" if cipher is None else "cipher:" + str(cipher.key_id)[:64]
        self.max_messages_per_session = max_messages_per_session
        self.retention_s = float(retention_days) * 86400.0
        self.clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA secure_delete=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._restrict_permissions()
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS companion_store_meta (
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS companion_turns (
                turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                generation INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_companion_turns_session_time
                ON companion_turns(session_id, timestamp DESC, turn_id DESC);
        """)
        self._check_store_mode()
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Best-effort owner-only permissions for SQLite and transient WAL files."""
        if not self.restrict_file_permissions or os.name == "nt" or self.path == ":memory:" or self.path.startswith("file:"):
            return
        base = Path(self.path)
        for candidate in (base, Path(self.path + "-wal"), Path(self.path + "-shm")):
            try:
                if candidate.exists() and not candidate.is_symlink() and candidate.is_file():
                    os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # Permission management can be delegated to the host/container policy.
                # The store still does not claim to be encrypted when this fails.
                pass

    def _check_store_mode(self) -> None:
        row = self._conn.execute("SELECT meta_value FROM companion_store_meta WHERE meta_key='store_mode'").fetchone()
        if row is not None:
            if str(row["meta_value"]) != self._store_mode:
                self._conn.close()
                raise SessionStoreError("transcript_store_mode_mismatch; use a new database or explicit migration")
            return
        count = int(self._conn.execute("SELECT COUNT(*) FROM companion_turns").fetchone()[0])
        if count and self.cipher is not None:
            self._conn.close()
            raise SessionStoreError("cannot_enable_encryption_on_existing_plaintext_store; migrate into a new database")
        self._conn.execute("INSERT INTO companion_store_meta(meta_key,meta_value) VALUES('store_mode',?)", (self._store_mode,))

    def save_turn(self, session_id: str, role: str, content: str, generation: int,
                  timestamp: float | None = None) -> bool:
        if not self.enabled:
            return False
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise ValueError("session_id must contain 1..200 characters")
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        if not isinstance(content, str) or len(content) > 8000:
            raise ValueError("content must be a string up to 8000 characters")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        ts = float(self.clock() if timestamp is None else timestamp)
        if not math.isfinite(ts):
            raise ValueError("timestamp must be finite")
        stored_content = self.cipher.encrypt(content) if self.cipher is not None else content
        if not isinstance(stored_content, str) or not stored_content:
            raise SessionStoreError("transcript_cipher_returned_invalid_content")
        cutoff = ts - self.retention_s
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM companion_turns WHERE timestamp < ?", (cutoff,))
                self._conn.execute("INSERT INTO companion_turns(session_id,role,content,generation,timestamp) VALUES(?,?,?,?,?)",
                                   (session_id, role, stored_content, generation, ts))
                self._conn.execute(
                    "DELETE FROM companion_turns WHERE session_id=? AND turn_id NOT IN "
                    "(SELECT turn_id FROM companion_turns WHERE session_id=? ORDER BY turn_id DESC LIMIT ?)",
                    (session_id, session_id, self.max_messages_per_session),
                )
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
        return True

    def load_session(self, session_id: str, *, limit: int = 64) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise ValueError("session_id must contain 1..200 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be in [1,1000]")
        limit = min(limit, self.max_messages_per_session)
        cutoff = float(self.clock()) - self.retention_s
        with self._lock:
            # Retention means physical deletion from this database, not just hiding old
            # rows from retrieval. Backup/replica retention remains the host's policy.
            self._conn.execute("DELETE FROM companion_turns WHERE timestamp < ?", (cutoff,))
            rows = self._conn.execute(
                "SELECT role,content,generation,timestamp FROM companion_turns "
                "WHERE session_id=? AND timestamp>=? ORDER BY turn_id DESC LIMIT ?",
                (session_id, cutoff, limit),
            ).fetchall()
        restored = []
        for row in reversed(rows):
            stored_content = str(row["content"])
            try:
                content = self.cipher.decrypt(stored_content) if self.cipher is not None else stored_content
            except Exception as exc:
                raise SessionStoreError("transcript_decryption_failed_or_store_corrupt") from exc
            restored.append({"role": row["role"], "content": content,
                             "generation": int(row["generation"]), "timestamp": float(row["timestamp"])})
        return restored

    def clear_session(self, session_id: str) -> int:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise ValueError("session_id must contain 1..200 characters")
        with self._lock:
            cursor = self._conn.execute("DELETE FROM companion_turns WHERE session_id=?", (session_id,))
            self._conn.execute("PRAGMA secure_delete=ON")
            return int(cursor.rowcount)

    def purge_expired(self) -> int:
        cutoff = float(self.clock()) - self.retention_s
        with self._lock:
            cursor = self._conn.execute("DELETE FROM companion_turns WHERE timestamp < ?", (cutoff,))
            self._conn.execute("PRAGMA secure_delete=ON")
            return int(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
