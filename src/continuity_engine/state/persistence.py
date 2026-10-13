"""SQLite persistence for event records and validated EngineState snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import os
import stat
import sqlite3
import threading
import math
from typing import Any, Iterator, Mapping

from ..core.models import DriveState, EngineState, Event, Intent, IntentStatus, WorldFact, WorldStatus, validate_json_compatible
from .schema_migration import EventSchemaMigrator, SchemaMigrationError
from .prediction import PredictionModel, PredictionObservation


class PersistenceError(RuntimeError):
    pass


class SQLiteRepository:
    def __init__(self, path: str | Path = ":memory:", *, restrict_file_permissions: bool = True):
        if not isinstance(restrict_file_permissions, bool):
            raise ValueError("restrict_file_permissions must be bool")
        self.path = str(path)
        self.restrict_file_permissions = restrict_file_permissions
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA secure_delete = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
        self._create_schema()
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Best-effort owner-only permissions for personal memory/state SQLite files."""
        if not self.restrict_file_permissions or os.name == "nt" or self.path == ":memory:" or self.path.startswith("file:"):
            return
        base = Path(self.path)
        for candidate in (base, Path(self.path + "-wal"), Path(self.path + "-shm")):
            try:
                if candidate.exists() and not candidate.is_symlink() and candidate.is_file():
                    os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # Deployments may rely on container/volume ACLs; this is best effort,
                # not encryption or a replacement for host access controls.
                pass

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS event_records (
                    event_id TEXT PRIMARY KEY,
                    event_schema_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantined_events (
                    event_id TEXT PRIMARY KEY,
                    original_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engine_snapshots (
                    snapshot_id INTEGER PRIMARY KEY CHECK(snapshot_id=1),
                    state_version INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS derived_items (
                    derived_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    partition_key TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prediction_observations (
                    partition_key TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    memory_id TEXT,
                    value_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    timestamp_mono REAL NOT NULL,
                    PRIMARY KEY(partition_key, event_id)
                );
                CREATE TABLE IF NOT EXISTS prediction_rebuild_jobs (
                    partition_key TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save_event(self, event: Event) -> None:
        payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO event_records(event_id,event_schema_version,payload_json,status,created_at) VALUES(?,?,?,?,?)",
                         (event.event_id, event.event_schema_version, payload, "ACTIVE", event.timestamp_wall))

    def load_events_with_migration(self, migrator: EventSchemaMigrator) -> list[Event]:
        """Migrate rows in one transaction. Unsupported events are quarantined unchanged."""
        valid: list[Event] = []
        with self.transaction() as conn:
            rows = conn.execute("SELECT event_id,event_schema_version,payload_json,status,created_at FROM event_records WHERE status='ACTIVE' ORDER BY created_at,event_id").fetchall()
            updates: list[tuple[str, str, str]] = []
            quarantines: list[tuple[str, str, str, float]] = []
            for row in rows:
                original = row["payload_json"]
                try:
                    migrated = migrator.migrate_dict(json.loads(original))
                    event = Event.from_dict(migrated)
                    updates.append((event.event_schema_version, json.dumps(migrated, ensure_ascii=False, separators=(",", ":"), sort_keys=True), event.event_id))
                    valid.append(event)
                except (SchemaMigrationError, ValueError, TypeError, KeyError) as exc:
                    quarantines.append((row["event_id"], original, f"{type(exc).__name__}:{exc}", row["created_at"]))
            for version, payload, event_id in updates:
                conn.execute("UPDATE event_records SET event_schema_version=?,payload_json=? WHERE event_id=?", (version, payload, event_id))
            for event_id, raw, reason, created_at in quarantines:
                conn.execute("INSERT OR REPLACE INTO quarantined_events(event_id,original_json,reason,quarantined_at) VALUES(?,?,?,?)",
                             (event_id, raw, reason, created_at))
                conn.execute("UPDATE event_records SET status='QUARANTINED' WHERE event_id=?", (event_id,))
        return valid

    def quarantined_event_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM quarantined_events").fetchone()
            return int(row["n"])

    def save_state(self, state: EngineState, now_wall: float) -> None:
        state.validate()
        payload = json.dumps(_state_to_dict(state), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
        with self.transaction() as conn:
            conn.execute("INSERT INTO engine_snapshots(snapshot_id,state_version,schema_version,payload_json,updated_at) VALUES(1,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET state_version=excluded.state_version,schema_version=excluded.schema_version,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                         (state.state_version, "1.2", payload, now_wall))

    def load_state(self) -> EngineState | None:
        with self._lock:
            row = self._conn.execute("SELECT payload_json,schema_version FROM engine_snapshots WHERE snapshot_id=1").fetchone()
        if row is None:
            return None
        if row["schema_version"] not in {"1.0", "1.1", "1.2"}:
            raise PersistenceError(f"unsupported engine snapshot schema {row['schema_version']}")
        try:
            state = _state_from_dict(json.loads(row["payload_json"]))
            state.validate()
            return state
        except Exception as exc:
            raise PersistenceError(f"invalid persisted state; refuse automatic active restore: {exc}") from exc

    def put_memory(self, memory_id: str, kind: str, payload: Mapping[str, Any], source_ids: list[str], created_at: float) -> None:
        if not memory_id or not kind:
            raise ValueError("memory_id and kind are required")
        validate_json_compatible(payload, "memory.payload")
        if not isinstance(source_ids, list) or any(not isinstance(x, str) for x in source_ids):
            raise ValueError("memory source_ids must be a list of strings")
        if not math.isfinite(created_at):
            raise ValueError("created_at must be finite")
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO memories(memory_id,kind,payload_json,source_ids_json,created_at,deleted) VALUES(?,?,?,?,?,0)",
                         (memory_id, kind, json.dumps(dict(payload), ensure_ascii=False, allow_nan=False), json.dumps(sorted(set(source_ids))), created_at))
            # If a caller reuses an ID to update content, an old vector must not survive.
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))

    def list_memories(self, *, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List live memories without exposing deleted rows or internal SQL records."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in [1,1000]")
        with self._lock:
            if kind is None:
                rows = self._conn.execute("SELECT memory_id,kind,payload_json,source_ids_json,created_at FROM memories WHERE deleted=0 ORDER BY created_at DESC,memory_id LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._conn.execute("SELECT memory_id,kind,payload_json,source_ids_json,created_at FROM memories WHERE deleted=0 AND kind=? ORDER BY created_at DESC,memory_id LIMIT ?", (kind, limit)).fetchall()
        return [{"memory_id": r["memory_id"], "kind": r["kind"], "payload": json.loads(r["payload_json"]),
                 "source_ids": json.loads(r["source_ids_json"]), "created_at": float(r["created_at"])} for r in rows]

    def search_memories(self, query: str, *, kinds: list[str] | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Small local lexical retrieval; it never stores the query and returns provenance."""
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1..2000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer in [1,20]")
        import re
        def terms(text: str) -> set[str]:
            normalized = text.casefold()
            latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
            # Overlapping CJK bigrams provide a dependency-free fallback tokenizer.
            cjk = "".join(ch for ch in normalized if "\u3400" <= ch <= "\u9fff")
            latin.update(cjk[i:i+2] for i in range(max(0, len(cjk)-1)))
            # Keep unigrams as well so short CJK queries (e.g. “猫”) can match a
            # longer phrase. Ranking/limit still bound broad matches.
            latin.update(cjk)
            return latin
        qterms = terms(query)
        if not qterms:
            return []
        with self._lock:
            rows = self._conn.execute("SELECT memory_id,kind,payload_json,source_ids_json,created_at FROM memories WHERE deleted=0 ORDER BY created_at DESC").fetchall()
        scored = []
        for row in rows:
            if kinds and row["kind"] not in kinds:
                continue
            payload = json.loads(row["payload_json"])
            searchable = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            hits = qterms & terms(searchable)
            if hits:
                score = len(hits) / max(1, len(qterms))
                scored.append((score, float(row["created_at"]), row, payload))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]["memory_id"]))
        return [{"memory_id": r["memory_id"], "kind": r["kind"], "payload": payload,
                 "source_ids": json.loads(r["source_ids_json"]), "created_at": float(r["created_at"]),
                 "retrieval_score": score} for score, _created, r, payload in scored[:limit]]

    def update_memory(self, memory_id: str, kind: str, payload: Mapping[str, Any], created_at: float) -> None:
        """Update memory content while preserving its existing provenance lineage."""
        if not kind or not math.isfinite(created_at):
            raise ValueError("kind must be non-empty and created_at finite")
        validate_json_compatible(payload, "memory.payload")
        with self.transaction() as conn:
            row = conn.execute("SELECT source_ids_json FROM memories WHERE memory_id=? AND deleted=0", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            conn.execute("UPDATE memories SET kind=?,payload_json=?,created_at=? WHERE memory_id=?",
                         (kind, json.dumps(dict(payload), ensure_ascii=False, allow_nan=False), created_at, memory_id))
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))

    def correct_memory(self, memory_id: str, kind: str, payload: Mapping[str, Any],
                       source_id: str, created_at: float) -> None:
        """Correct a memory while retaining original provenance and appending the correction source."""
        if not memory_id or not kind or not isinstance(source_id, str) or not source_id or not math.isfinite(created_at):
            raise ValueError("invalid memory correction metadata")
        validate_json_compatible(payload, "memory.payload")
        with self.transaction() as conn:
            row = conn.execute("SELECT source_ids_json FROM memories WHERE memory_id=? AND deleted=0", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            try:
                sources = set(json.loads(row["source_ids_json"]))
            except (ValueError, TypeError):
                sources = set()
            sources.add(source_id)
            conn.execute("UPDATE memories SET kind=?,payload_json=?,source_ids_json=?,created_at=? WHERE memory_id=?",
                         (kind, json.dumps(dict(payload), ensure_ascii=False, allow_nan=False),
                          json.dumps(sorted(sources)), created_at, memory_id))
            # A corrected fact must not retain its old semantic vector.
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))

    def set_memory_embedding(self, memory_id: str, model_id: str, embedding: list[float], updated_at: float) -> None:
        """Persist a finite, normalized-or-raw vector without tying it to a model vendor."""
        if not isinstance(memory_id, str) or not memory_id or not isinstance(model_id, str) or not model_id or len(model_id) > 200:
            raise ValueError("memory_id and model_id are required")
        if not isinstance(embedding, list) or not 8 <= len(embedding) <= 16384:
            raise ValueError("embedding dimensions must be in [8,16384]")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in embedding):
            raise ValueError("embedding values must be finite numbers")
        if not math.isfinite(updated_at):
            raise ValueError("updated_at must be finite")
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM memories WHERE memory_id=? AND deleted=0", (memory_id,)).fetchone()
            if exists is None:
                raise KeyError(memory_id)
            conn.execute("INSERT INTO memory_embeddings(memory_id,model_id,dimensions,embedding_json,updated_at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(memory_id) DO UPDATE SET model_id=excluded.model_id,dimensions=excluded.dimensions,embedding_json=excluded.embedding_json,updated_at=excluded.updated_at",
                         (memory_id, model_id, len(embedding), json.dumps([float(v) for v in embedding], separators=(",", ":"), allow_nan=False), updated_at))

    def get_memory_embedding(self, memory_id: str, *, model_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if model_id is None:
                row = self._conn.execute("SELECT model_id,dimensions,embedding_json,updated_at FROM memory_embeddings WHERE memory_id=?", (memory_id,)).fetchone()
            else:
                row = self._conn.execute("SELECT model_id,dimensions,embedding_json,updated_at FROM memory_embeddings WHERE memory_id=? AND model_id=?", (memory_id, model_id)).fetchone()
        if row is None:
            return None
        try:
            vector = json.loads(row["embedding_json"])
            if not isinstance(vector, list) or len(vector) != int(row["dimensions"]):
                return None
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in vector):
                return None
            return {"memory_id": memory_id, "model_id": row["model_id"], "dimensions": int(row["dimensions"]),
                    "embedding": [float(v) for v in vector], "updated_at": float(row["updated_at"])}
        except (ValueError, TypeError, KeyError):
            return None

    def get_memory_embeddings(self, memory_ids: list[str], *, model_id: str) -> dict[str, dict[str, Any]]:
        """Fetch a bounded batch of model-scoped vectors with one SQLite query."""
        if not isinstance(memory_ids, list) or len(memory_ids) > 10000 or any(not isinstance(x, str) for x in memory_ids):
            raise ValueError("memory_ids must be a list of at most 10000 strings")
        if not isinstance(model_id, str) or not model_id or len(model_id) > 200:
            raise ValueError("model_id is required and must be <= 200 characters")
        ids = list(dict.fromkeys(x for x in memory_ids if x))
        if not ids:
            return {}
        output: dict[str, dict[str, Any]] = {}
        # SQLite builds commonly cap bound variables at 999; chunk below that bound.
        with self._lock:
            for start in range(0, len(ids), 900):
                batch = ids[start:start + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(
                    f"SELECT memory_id,model_id,dimensions,embedding_json,updated_at FROM memory_embeddings "
                    f"WHERE model_id=? AND memory_id IN ({placeholders})", (model_id, *batch)
                ).fetchall()
                for row in rows:
                    try:
                        vector = json.loads(row["embedding_json"])
                        if not isinstance(vector, list) or len(vector) != int(row["dimensions"]):
                            continue
                        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in vector):
                            continue
                        memory_id = str(row["memory_id"])
                        output[memory_id] = {"memory_id": memory_id, "model_id": row["model_id"],
                                             "dimensions": int(row["dimensions"]),
                                             "embedding": [float(v) for v in vector],
                                             "updated_at": float(row["updated_at"])}
                    except (ValueError, TypeError, KeyError, OverflowError):
                        continue
        return output

    def delete_memory_embedding(self, memory_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
            return cursor.rowcount > 0

    def put_derived(self, derived_id: str, kind: str, payload: Mapping[str, Any], source_ids: list[str], partition_key: str | None, created_at: float) -> None:
        if not derived_id or not kind:
            raise ValueError("derived_id and kind are required")
        validate_json_compatible(payload, "derived.payload")
        if not isinstance(source_ids, list) or any(not isinstance(x, str) for x in source_ids):
            raise ValueError("derived source_ids must be a list of strings")
        if not math.isfinite(created_at):
            raise ValueError("created_at must be finite")
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO derived_items(derived_id,kind,payload_json,source_ids_json,partition_key,created_at) VALUES(?,?,?,?,?,?)",
                         (derived_id, kind, json.dumps(dict(payload), ensure_ascii=False, allow_nan=False), json.dumps(sorted(set(source_ids))), partition_key, created_at))

    def put_prediction_observation(self, partition_key: str, event_id: str, memory_id: str | None, value: Any, confidence: float, timestamp_mono: float) -> None:
        if not partition_key or not event_id:
            raise ValueError("partition_key and event_id are required")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("prediction value must be finite numeric")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and in [0,1]")
        if not math.isfinite(timestamp_mono):
            raise ValueError("timestamp_mono must be finite")
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO prediction_observations(partition_key,event_id,memory_id,value_json,confidence,timestamp_mono) VALUES(?,?,?,?,?,?)",
                         (partition_key, event_id, memory_id, json.dumps(float(value), ensure_ascii=False, allow_nan=False), confidence, timestamp_mono))

    def load_prediction_observations(self, partition_key: str) -> list[PredictionObservation]:
        """Load authoritative remaining observations for one prediction partition."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id,memory_id,value_json,confidence,timestamp_mono FROM prediction_observations WHERE partition_key=? ORDER BY timestamp_mono,event_id",
                (partition_key,),
            ).fetchall()
        observations: list[PredictionObservation] = []
        for row in rows:
            value = json.loads(row["value_json"])
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                raise PersistenceError(f"invalid persisted prediction value in partition {partition_key}")
            observations.append(PredictionObservation(row["event_id"], row["memory_id"], partition_key,
                                                     float(value), float(row["confidence"]), float(row["timestamp_mono"])))
        return observations

    def delete_memory(self, memory_id: str, now_wall: float, *, prediction_model: PredictionModel | None = None) -> dict[str, Any]:
        """Delete a memory and lineage descendants, then rebuild in-memory prediction state.

        If affected partitions exist but no model is supplied, status is
        PENDING_DERIVED_REBUILD: the deletion is committed in storage, but callers must
        not report the operation as fully complete until the live model is rebuilt/reset.
        When rebuilding fails, the affected live partition is reset fail-closed.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT memory_id FROM memories WHERE memory_id=? AND deleted=0", (memory_id,)).fetchone()
            if row is None:
                return {"status": "NOT_FOUND", "memory_id": memory_id, "deleted_memory_ids": [],
                        "removed_derived": 0, "affected_prediction_partitions": []}

            # Compute transitive descendants before deleting content. A summary based on a
            # summary must not survive deletion of the original source memory.
            delete_ids = {memory_id}
            all_memories = conn.execute("SELECT memory_id,source_ids_json FROM memories WHERE deleted=0").fetchall()
            changed = True
            while changed:
                changed = False
                for candidate in all_memories:
                    cid = candidate["memory_id"]
                    if cid in delete_ids:
                        continue
                    try:
                        sources = set(json.loads(candidate["source_ids_json"]))
                    except json.JSONDecodeError:
                        # Corrupt lineage: cannot prove independence; quarantine/delete derived item.
                        sources = set(delete_ids)
                    if sources & delete_ids:
                        delete_ids.add(cid)
                        changed = True

            for target_id in delete_ids:
                conn.execute("UPDATE memories SET deleted=1 WHERE memory_id=?", (target_id,))
                conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (target_id,))

            derived_rows = conn.execute("SELECT derived_id,source_ids_json FROM derived_items").fetchall()
            to_delete = []
            for derived in derived_rows:
                try:
                    source_ids = set(json.loads(derived["source_ids_json"]))
                except json.JSONDecodeError:
                    source_ids = set(delete_ids)  # fail closed for corrupted provenance
                if source_ids & delete_ids:
                    to_delete.append(derived["derived_id"])
            for derived_id in to_delete:
                conn.execute("DELETE FROM derived_items WHERE derived_id=?", (derived_id,))

            placeholders = ",".join("?" for _ in delete_ids)
            partition_rows = conn.execute(
                f"SELECT DISTINCT partition_key FROM prediction_observations WHERE memory_id IN ({placeholders}) OR event_id IN ({placeholders})",
                (*delete_ids, *delete_ids),
            ).fetchall()
            partitions = sorted({str(r["partition_key"]) for r in partition_rows})
            for partition in partitions:
                conn.execute(
                    "INSERT INTO prediction_rebuild_jobs(partition_key,reason,created_at) VALUES(?,?,?) "
                    "ON CONFLICT(partition_key) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at",
                    (partition, "memory_deletion", now_wall),
                )
            conn.execute(
                f"DELETE FROM prediction_observations WHERE memory_id IN ({placeholders}) OR event_id IN ({placeholders})",
                (*delete_ids, *delete_ids),
            )
            conn.execute(f"DELETE FROM memories WHERE memory_id IN ({placeholders})", tuple(delete_ids))

        rebuild_results: dict[str, str] = {}
        if partitions and prediction_model is None:
            status = "PENDING_DERIVED_REBUILD"
        elif partitions:
            for partition in partitions:
                try:
                    observations = self.load_prediction_observations(partition)
                    rebuild_results[partition] = prediction_model.replace_partition(partition, observations)
                except Exception:
                    # Deletion must not leave potentially contaminated live estimates.
                    prediction_model.reset_partition(partition)
                    rebuild_results[partition] = "RESET_AFTER_REBUILD_ERROR"
                # REBUILT or RESET is safe: remove the durable repair job only after the
                # in-memory partition no longer contains potentially deleted evidence.
                with self.transaction() as conn:
                    conn.execute("DELETE FROM prediction_rebuild_jobs WHERE partition_key=?", (partition,))
            status = "DELETED"
        else:
            status = "DELETED"
        return {"status": status, "memory_id": memory_id, "deleted_memory_ids": sorted(delete_ids),
                "removed_derived": len(to_delete), "affected_prediction_partitions": partitions,
                "prediction_rebuild_results": rebuild_results,
                "requires_rebuild": bool(partitions and prediction_model is None), "completed_at": now_wall}

    def pending_prediction_rebuilds(self) -> list[str]:
        """Return durable prediction partitions that must be rebuilt after a deletion."""
        with self._lock:
            rows = self._conn.execute("SELECT partition_key FROM prediction_rebuild_jobs ORDER BY partition_key").fetchall()
            return [str(row["partition_key"]) for row in rows]

    def reconcile_prediction_model(self, prediction_model: PredictionModel) -> dict[str, str]:
        """Rebuild pending live-model partitions from authoritative remaining DB rows.

        Must run before the application allows prediction-dependent behavior after startup
        whenever pending jobs exist. Corrupt evidence causes a conservative partition reset.
        """
        outcomes: dict[str, str] = {}
        for partition in self.pending_prediction_rebuilds():
            try:
                rows = self.load_prediction_observations(partition)
                outcomes[partition] = prediction_model.replace_partition(partition, rows)
            except Exception:
                prediction_model.reset_partition(partition)
                outcomes[partition] = "RESET_AFTER_REBUILD_ERROR"
            with self.transaction() as conn:
                conn.execute("DELETE FROM prediction_rebuild_jobs WHERE partition_key=?", (partition,))
        return outcomes


def _state_to_dict(state: EngineState) -> dict[str, Any]:
    intent = None
    if state.current_intent is not None:
        i = state.current_intent
        intent = {
            "intent_id": i.intent_id, "intent_type": i.intent_type, "goal": i.goal, "status": i.status.value,
            "score": i.score, "created_at_mono": i.created_at_mono, "started_at_mono": i.started_at_mono,
            "reason": i.reason, "action_type": i.action_type, "action_args": i.action_args,
            "deadline_mono": i.deadline_mono, "clarification_rounds": i.clarification_rounds,
            "clarification_questions": i.clarification_questions, "generation": i.generation,
            "cancellation_requested": i.cancellation_requested, "interruption_policy": i.interruption_policy,
            "ordinary_proactive": i.ordinary_proactive, "is_safety_alert": i.is_safety_alert,
        }
    return {
        "state_version": state.state_version, "config_version": state.config_version,
        "world": {key: {"key": f.key, "value": f.value, "status": f.status.value, "confidence": f.confidence,
                          "sources": list(f.sources), "updated_at_mono": f.updated_at_mono,
                          "expires_at_mono": f.expires_at_mono, "evidence_event_ids": list(f.evidence_event_ids)}
                   for key, f in state.world.items()},
        "drives": {key: {"value": d.value, "target": d.target, "urgency": d.urgency, "updated_at_mono": d.updated_at_mono} for key, d in state.drives.items()},
        "current_intent": intent, "quiet_scope": state.quiet_scope, "quiet_until_wall": state.quiet_until_wall,
        "output_muted": state.output_muted, "user_speaking": state.user_speaking, "safety_stopped": state.safety_stopped,
        "last_user_message": state.last_user_message, "generation_by_interaction": state.generation_by_interaction,
        "last_reason": state.last_reason, "task_state": state.task_state, "life_layer_state": state.life_layer_state,
    }


def _state_from_dict(data: Mapping[str, Any]) -> EngineState:
    state = EngineState(state_version=int(data["state_version"]), config_version=int(data["config_version"]))
    state.world = {key: WorldFact(
        key=f["key"], value=f["value"], status=WorldStatus(f["status"]), confidence=float(f["confidence"]),
        sources=tuple(f["sources"]), updated_at_mono=float(f["updated_at_mono"]),
        expires_at_mono=float(f["expires_at_mono"]), evidence_event_ids=tuple(f.get("evidence_event_ids", [])))
        for key, f in data.get("world", {}).items()}
    state.drives = {key: DriveState(float(d["value"]), float(d["target"]), float(d["urgency"]), float(d["updated_at_mono"]))
                    for key, d in data.get("drives", {}).items()}
    i = data.get("current_intent")
    if i is not None:
        state.current_intent = Intent(
            intent_id=i["intent_id"], intent_type=i["intent_type"], goal=i["goal"], status=IntentStatus(i["status"]),
            score=float(i["score"]), created_at_mono=float(i["created_at_mono"]), started_at_mono=float(i["started_at_mono"]),
            reason=i["reason"], action_type=i.get("action_type"), action_args=dict(i.get("action_args", {})),
            deadline_mono=i.get("deadline_mono"), clarification_rounds=int(i.get("clarification_rounds", 0)),
            clarification_questions=list(i.get("clarification_questions", [])), generation=int(i.get("generation", 0)),
            cancellation_requested=bool(i.get("cancellation_requested", False)), interruption_policy=i.get("interruption_policy", "REPLAN"),
            ordinary_proactive=bool(i.get("ordinary_proactive", False)), is_safety_alert=bool(i.get("is_safety_alert", False)))
    state.quiet_scope = data.get("quiet_scope")
    state.quiet_until_wall = data.get("quiet_until_wall")
    state.output_muted = bool(data.get("output_muted", False))
    state.user_speaking = bool(data.get("user_speaking", False))
    state.safety_stopped = bool(data.get("safety_stopped", False))
    state.last_user_message = data.get("last_user_message")
    state.generation_by_interaction = {str(k): int(v) for k, v in data.get("generation_by_interaction", {}).items()}
    state.last_reason = str(data.get("last_reason", "restored"))
    # Snapshot schema 1.0 predates task_state; the additive migration defaults it empty.
    state.task_state = dict(data.get("task_state", {}))
    state.life_layer_state = dict(data.get("life_layer_state", {}))
    return state
