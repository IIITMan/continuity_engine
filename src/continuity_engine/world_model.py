"""A small evidence-based world model with explicit stale/unknown/conflict states."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import json
import math
from typing import Any

from .models import Evidence, WorldFact, WorldStatus, thaw, validate_json_compatible


@dataclass(frozen=True)
class WorldModelConfig:
    confirm_confidence: float = 0.90
    probable_confidence: float = 0.55
    conflict_confidence: float = 0.60
    max_evidence_per_source_key: int = 32

    def __post_init__(self) -> None:
        for name, value in (("confirm_confidence", self.confirm_confidence),
                            ("probable_confidence", self.probable_confidence),
                            ("conflict_confidence", self.conflict_confidence)):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0,1]")
        if self.probable_confidence > self.confirm_confidence:
            raise ValueError("probable_confidence cannot exceed confirm_confidence")
        if self.max_evidence_per_source_key < 1:
            raise ValueError("max_evidence_per_source_key must be >= 1")


class WorldModel:
    """Stores latest evidence per (fact key, source), never averages raw confidences.

    This is a conservative baseline, not a calibrated Bayesian sensor fusion model.
    Contradictory sufficiently-confident sources produce CONFLICT.
    """

    def __init__(self, config: WorldModelConfig | None = None):
        self.config = config or WorldModelConfig()
        self._evidence: dict[str, dict[str, Evidence]] = defaultdict(dict)
        self._event_ids: set[str] = set()
        self._event_id_order: deque[str] = deque(maxlen=100_000)
        self._facts: dict[str, WorldFact] = {}

    def observe(
        self, *, key: str, value: Any, confidence: float, source: str,
        observed_at_mono: float, ttl_s: float, event_id: str,
    ) -> WorldFact:
        if not key or not source or not event_id:
            raise ValueError("key, source and event_id are required")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and in [0,1]")
        if not math.isfinite(observed_at_mono) or not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError("observation time must be finite and ttl_s > 0")
        if event_id in self._event_ids:
            return self.get(key, observed_at_mono)
        value = thaw(value)
        validate_json_compatible(value, "world.value")
        expires_at = observed_at_mono + ttl_s
        if not math.isfinite(expires_at):
            raise ValueError("observation expiry must be finite")
        evidence = Evidence(key, value, confidence, source, observed_at_mono, expires_at, event_id)
        by_source = self._evidence[key]
        previous = by_source.get(source)
        if previous is not None and observed_at_mono < previous.observed_at_mono:
            # Out-of-order telemetry is recorded as seen but cannot roll back current world state.
            self._event_ids.add(event_id)
            self._event_id_order.append(event_id)
            if len(self._event_ids) > self._event_id_order.maxlen:
                self._event_ids = set(self._event_id_order)
            latest_known_time = max((item.observed_at_mono for item in by_source.values()), default=observed_at_mono)
            fact = self._recompute(key, latest_known_time)
            self._facts[key] = fact
            return fact
        by_source[source] = evidence  # latest observation from a source replaces its previous value
        if len(by_source) > self.config.max_evidence_per_source_key:
            # The dict is latest-per-source; this branch is defensive if storage semantics change.
            oldest_source = min(by_source, key=lambda s: by_source[s].observed_at_mono)
            del by_source[oldest_source]
        self._event_ids.add(event_id)
        self._event_id_order.append(event_id)
        if len(self._event_ids) > self._event_id_order.maxlen:
            self._event_ids = set(self._event_id_order)
        fact = self._recompute(key, observed_at_mono)
        self._facts[key] = fact
        return fact

    def _recompute(self, key: str, now_mono: float) -> WorldFact:
        all_evidence = list(self._evidence.get(key, {}).values())
        live = [e for e in all_evidence if e.expires_at_mono > now_mono]
        if not live:
            old = self._facts.get(key)
            if old is None:
                return WorldFact(key, None, WorldStatus.UNKNOWN, 0.0, (), now_mono, now_mono, ())
            return WorldFact(key, None, WorldStatus.STALE, 0.0, old.sources, now_mono, now_mono, old.evidence_event_ids)

        by_value: dict[str, list[Evidence]] = defaultdict(list)
        for evidence in live:
            # Canonical JSON avoids object-dict insertion order creating false conflicts.
            value_key = json.dumps(evidence.value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            by_value[value_key].append(evidence)
        strong_groups = [group for group in by_value.values() if max(e.confidence for e in group) >= self.config.conflict_confidence]
        if len(strong_groups) > 1:
            sources = tuple(sorted({e.source for group in strong_groups for e in group}))
            ids = tuple(sorted({e.event_id for group in strong_groups for e in group}))
            return WorldFact(key, None, WorldStatus.CONFLICT, max(e.confidence for e in live), sources,
                             max(e.observed_at_mono for e in live), min(e.expires_at_mono for e in live), ids)

        # If no conflict, choose the strongest evidence, recency breaks ties. No arithmetic averaging.
        chosen = max(live, key=lambda e: (e.confidence, e.observed_at_mono))
        if chosen.confidence >= self.config.confirm_confidence:
            status = WorldStatus.CONFIRMED
        elif chosen.confidence >= self.config.probable_confidence:
            status = WorldStatus.PROBABLE
        else:
            status = WorldStatus.UNKNOWN
        chosen_key = json.dumps(chosen.value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        supporting = [e for e in live if json.dumps(e.value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) == chosen_key]
        return WorldFact(key, chosen.value, status, chosen.confidence,
                         tuple(sorted({e.source for e in supporting})),
                         max(e.observed_at_mono for e in supporting),
                         max(e.expires_at_mono for e in supporting),
                         tuple(sorted({e.event_id for e in supporting})))

    def get(self, key: str, now_mono: float) -> WorldFact:
        fact = self._recompute(key, now_mono)
        self._facts[key] = fact
        return fact

    def snapshot(self, now_mono: float) -> dict[str, WorldFact]:
        for key in list(self._evidence):
            self.get(key, now_mono)
        return dict(self._facts)

    def clear(self) -> None:
        self._evidence.clear()
        self._event_ids.clear()
        self._event_id_order.clear()
        self._facts.clear()

    def restore_facts(self, facts: dict[str, WorldFact]) -> None:
        """Restore conservatively stale facts after reboot; no old-boot evidence is loaded."""
        self._evidence.clear()
        self._event_ids.clear()
        self._event_id_order.clear()
        self._facts = dict(facts)
