"""Simple confidence-weighted prediction statistics with lineage-aware deletion."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class PredictionObservation:
    event_id: str
    memory_id: str | None
    key: str
    value: float
    confidence: float
    timestamp_mono: float


@dataclass(frozen=True, slots=True)
class PredictionEstimate:
    key: str
    expected_value: float | None
    effective_confidence: float
    observation_count: int
    provisional: bool


class PredictionModel:
    """Deterministic EMA model. Deletion rebuilds impacted partitions from evidence."""

    def __init__(self, alpha: float = 0.1, min_observations: int = 3):
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0,1]")
        if min_observations < 1:
            raise ValueError("min_observations must be >= 1")
        self.alpha = alpha
        self.min_observations = min_observations
        self._observations: dict[str, list[PredictionObservation]] = {}
        self._expected: dict[str, float] = {}

    def learn(self, obs: PredictionObservation) -> PredictionEstimate:
        if not obs.event_id or not obs.key:
            raise ValueError("event_id and key are required")
        for n, v in (("value", obs.value), ("confidence", obs.confidence), ("timestamp_mono", obs.timestamp_mono)):
            if isinstance(v, bool) or not math.isfinite(v):
                raise ValueError(f"{n} must be finite numeric")
        if not 0 <= obs.confidence <= 1:
            raise ValueError("confidence must be in [0,1]")
        if obs.confidence <= 0:
            return self.estimate(obs.key)
        rows = self._observations.setdefault(obs.key, [])
        if any(row.event_id == obs.event_id for row in rows):
            return self.estimate(obs.key)
        rows.append(obs)
        old = self._expected.get(obs.key)
        # First reliable observation initializes; later updates scale alpha by evidence confidence.
        if old is None:
            self._expected[obs.key] = obs.value
        else:
            weight = self.alpha * obs.confidence
            self._expected[obs.key] = old + weight * (obs.value - old)
        return self.estimate(obs.key)

    def estimate(self, key: str) -> PredictionEstimate:
        rows = self._observations.get(key, [])
        expected = self._expected.get(key)
        count = len(rows)
        conf = (sum(r.confidence for r in rows) / count) if count else 0.0
        return PredictionEstimate(key, expected, conf, count, count >= self.min_observations)

    def delete_memory_lineage(self, memory_id: str, *, lineage_complete: bool = True) -> dict[str, str]:
        """Remove observations sourced by memory_id; rebuild or reset affected keys.

        Return maps partition keys to REBUILT or RESET. Incomplete lineage forces reset.
        """
        affected = [key for key, rows in self._observations.items() if any(r.memory_id == memory_id for r in rows)]
        result: dict[str, str] = {}
        for key in affected:
            rows = self._observations[key]
            if not lineage_complete:
                self._observations[key] = []
                self._expected.pop(key, None)
                result[key] = "RESET"
                continue
            remaining = [r for r in rows if r.memory_id != memory_id]
            self._observations[key] = remaining
            self._rebuild(key)
            result[key] = "REBUILT" if remaining else "RESET"
        return result

    def _rebuild(self, key: str) -> None:
        rows = self._observations.get(key, [])
        self._expected.pop(key, None)
        for row in rows:
            old = self._expected.get(key)
            if old is None:
                self._expected[key] = row.value
            else:
                weight = self.alpha * row.confidence
                self._expected[key] = old + weight * (row.value - old)


    def replace_partition(self, key: str, observations: list[PredictionObservation], *, lineage_complete: bool = True) -> str:
        """Replace one model partition from authoritative persisted observations.

        A caller deleting memory must not leave a cached estimate derived from that
        memory. If provenance is incomplete or replaying observations fails, reset the
        partition instead of retaining potentially contaminated statistics.
        """
        if not lineage_complete:
            self._observations[key] = []
            self._expected.pop(key, None)
            return "RESET"
        ordered = sorted(observations, key=lambda row: (row.timestamp_mono, row.event_id))
        if any(row.key != key for row in ordered):
            raise ValueError("all replacement observations must belong to the requested partition")
        try:
            # Validate everything before mutating the existing partition.
            for row in ordered:
                if not row.event_id or any(isinstance(v, bool) or not math.isfinite(v) for v in (row.value, row.confidence, row.timestamp_mono)):
                    raise ValueError("invalid prediction observation")
                if not 0 <= row.confidence <= 1:
                    raise ValueError("prediction confidence must be in [0,1]")
            self._observations[key] = []
            self._expected.pop(key, None)
            for row in ordered:
                if row.confidence > 0:
                    self._observations[key].append(row)
                    old = self._expected.get(key)
                    if old is None:
                        self._expected[key] = row.value
                    else:
                        self._expected[key] = old + (self.alpha * row.confidence) * (row.value - old)
        except Exception:
            self._observations[key] = []
            self._expected.pop(key, None)
            return "RESET"
        return "REBUILT" if self._observations[key] else "RESET"

    def reset_partition(self, key: str) -> None:
        """Forget all statistics and evidence held for one key."""
        self._observations.pop(key, None)
        self._expected.pop(key, None)

    def export_state(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "min_observations": self.min_observations,
            "observations": {
                key: [row.__dict__ if hasattr(row, "__dict__") else {
                    "event_id": row.event_id, "memory_id": row.memory_id, "key": row.key,
                    "value": row.value, "confidence": row.confidence, "timestamp_mono": row.timestamp_mono,
                } for row in rows]
                for key, rows in self._observations.items()
            },
            "expected": dict(self._expected),
        }
