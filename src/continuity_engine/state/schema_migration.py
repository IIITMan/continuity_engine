"""Explicit persisted event-schema migrations; never guess unknown schemas."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

from ..core.models import CURRENT_EVENT_SCHEMA, Event


class SchemaMigrationError(ValueError):
    pass


class UnknownSchemaVersion(SchemaMigrationError):
    pass


Migration = Callable[[dict[str, Any]], dict[str, Any]]


class EventSchemaMigrator:
    def __init__(self, current_version: str = CURRENT_EVENT_SCHEMA):
        self.current_version = current_version
        self._migrations: dict[str, tuple[str, Migration]] = {}
        self.register("0.9", "1.0", self._migrate_09_to_10)

    def register(self, from_version: str, to_version: str, fn: Migration) -> None:
        if from_version in self._migrations:
            raise ValueError(f"migration from {from_version} already registered")
        if from_version == to_version:
            raise ValueError("migration must change version")
        self._migrations[from_version] = (to_version, fn)

    def migrate_dict(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        data = deepcopy(dict(raw))
        version = data.get("event_schema_version")
        if not isinstance(version, str):
            raise UnknownSchemaVersion("missing event_schema_version; explicit legacy adapter required")
        seen: set[str] = set()
        while version != self.current_version:
            if version in seen:
                raise SchemaMigrationError(f"migration cycle at {version}")
            seen.add(version)
            migration = self._migrations.get(version)
            if migration is None:
                raise UnknownSchemaVersion(f"no migration registered from schema {version}")
            next_version, fn = migration
            data = fn(data)
            if not isinstance(data, dict):
                raise SchemaMigrationError("migration must return an object")
            data["event_schema_version"] = next_version
            version = next_version
        # Validate by constructing the canonical event DTO; only its serialized form exits.
        try:
            return Event.from_dict(data).to_dict()
        except Exception as exc:
            raise SchemaMigrationError(f"migrated event failed validation: {exc}") from exc

    @staticmethod
    def _migrate_09_to_10(data: dict[str, Any]) -> dict[str, Any]:
        migrated = deepcopy(data)
        aliases = {"id": "event_id", "ts_mono": "timestamp_mono", "ts_wall": "timestamp_wall"}
        for old, new in aliases.items():
            if new not in migrated and old in migrated:
                migrated[new] = migrated.pop(old)
        migrated.setdefault("priority", 50)
        migrated.setdefault("payload", {})
        migrated.setdefault("source", "legacy")
        if "kind" not in migrated:
            raise SchemaMigrationError("legacy event missing kind")
        return migrated
