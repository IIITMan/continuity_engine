"""Bounded ordinary event queue plus a separately reserved control lane."""
from __future__ import annotations

from collections import deque
import math
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable

from ..core.models import CONTROL_EVENT_KINDS, NO_SILENT_DROP_EVENT_KINDS, Event, EventKind, Priority


class ControlLaneFull(RuntimeError):
    """A protected event could not be queued; caller must enter degraded handling."""


@dataclass(frozen=True, slots=True)
class EnqueueReceipt:
    accepted: bool
    disposition: str
    event_id: str
    dropped_event_ids: tuple[str, ...] = ()


@dataclass
class ControlLatches:
    safety_stop: bool = False
    user_stop: bool = False
    muted: bool | None = None
    cancelled_intent_ids: set[str] = field(default_factory=set)
    quiet_scope: str | None = None
    quiet_changed: bool = False
    cancel_all: bool = False
    degraded: bool = False
    user_speaking: bool = False
    speech_started: bool = False
    cancelled_task_ids: set[str] = field(default_factory=set)
    cancel_all_tasks: bool = False
    paused_task_ids: set[str] = field(default_factory=set)
    pause_all_tasks: bool = False


class EventRouter:
    """In-memory router. Protected controls are prioritized and never silently lost.

    The latches are a fast-path signal; a control event also remains in the lane for
    audit and idempotent event handling. If the lane overflows, enqueue raises rather
    than falsely reporting success.
    """

    def __init__(self, max_queue_size: int = 2000, reserved_control_capacity: int = 64):
        if max_queue_size < 1 or reserved_control_capacity < 1:
            raise ValueError("queue capacities must be positive")
        self.max_queue_size = max_queue_size
        self.reserved_control_capacity = reserved_control_capacity
        self._normal: deque[Event] = deque()
        self._control: deque[Event] = deque()
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._seen_cache_size = max(10_000, max_queue_size * 4)
        self._lock = Lock()
        self.latches = ControlLatches()
        self.drop_counts: dict[str, int] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._normal) + len(self._control)

    def _mark_seen(self, event_id: str) -> None:
        if event_id in self._seen_ids:
            return
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > self._seen_cache_size:
            old = self._seen_order.popleft()
            self._seen_ids.discard(old)

    def _record_drop(self, event: Event, reason: str) -> None:
        self.drop_counts[reason] = self.drop_counts.get(reason, 0) + 1

    def _latch(self, event: Event) -> None:
        if event.kind == EventKind.SAFETY_STOP.value:
            self.latches.safety_stop = True
        elif event.kind == EventKind.USER_STOP.value:
            self.latches.user_stop = True
        elif event.kind == EventKind.USER_MUTE.value:
            self.latches.muted = True
        elif event.kind == EventKind.USER_UNMUTE.value:
            self.latches.muted = False
        elif event.kind == EventKind.USER_CANCEL.value:
            intent_id = event.payload.get("intent_id")
            if intent_id:
                self.latches.cancelled_intent_ids.add(str(intent_id))
            else:
                self.latches.cancel_all = True
        elif event.kind == EventKind.USER_QUIET.value:
            self.latches.quiet_changed = True
            if event.payload.get("enabled", True):
                scope = str(event.payload.get("scope", "SESSION")).upper()
                self.latches.quiet_scope = scope if scope in {"TURN", "SESSION", "PERSISTENT"} else "SESSION"
            else:
                self.latches.quiet_scope = None
        elif event.kind == EventKind.USER_SPEECH_STARTED.value:
            self.latches.user_speaking = True
            self.latches.speech_started = True
        elif event.kind == EventKind.USER_SPEECH_ENDED.value:
            self.latches.user_speaking = False
        elif event.kind == EventKind.TASK_CANCEL.value:
            task_id = event.payload.get("task_id")
            if task_id is None:
                self.latches.cancel_all_tasks = True
            else:
                self.latches.cancelled_task_ids.add(str(task_id))
        elif event.kind == EventKind.TASK_PAUSE.value:
            task_id = event.payload.get("task_id")
            if task_id is None:
                self.latches.pause_all_tasks = True
            else:
                self.latches.paused_task_ids.add(str(task_id))

    def enqueue(self, event: Event, now_mono: float) -> EnqueueReceipt:
        with self._lock:
            if event.event_id in self._seen_ids:
                return EnqueueReceipt(False, "duplicate_event_id", event.event_id)
            protected = event.kind in CONTROL_EVENT_KINDS
            if protected:
                # Set the fast control latch before attempting audit queue insertion.
                self._latch(event)
                if len(self._control) >= self.reserved_control_capacity:
                    self.latches.degraded = True
                    raise ControlLaneFull(f"reserved control lane full for {event.kind}; fail-closed handling required")
                self._control.append(event)
                self._mark_seen(event.event_id)
                return EnqueueReceipt(True, "reserved_control_lane", event.event_id)

            if event.expires_at_mono is not None and event.expires_at_mono <= now_mono:
                self._record_drop(event, "expired_incoming")
                self._mark_seen(event.event_id)
                return EnqueueReceipt(False, "expired_incoming", event.event_id, (event.event_id,))

            dropped: list[str] = []
            # First discard expired queued items.
            kept = deque()
            for old in self._normal:
                if old.expires_at_mono is not None and old.expires_at_mono <= now_mono:
                    dropped.append(old.event_id)
                    self._record_drop(old, "expired_queued")
                else:
                    kept.append(old)
            self._normal = kept

            # Coalesce replaceable state by explicit key; only same-source/key values replace.
            coalesce_key = event.payload.get("coalesce_key")
            if coalesce_key is not None:
                replaced = deque()
                for old in self._normal:
                    if old.kind == event.kind and old.source == event.source and old.payload.get("coalesce_key") == coalesce_key:
                        dropped.append(old.event_id)
                        self._record_drop(old, "coalesced_replaceable_state")
                    else:
                        replaced.append(old)
                self._normal = replaced

            if len(self._normal) >= self.max_queue_size:
                # Lowest-priority + oldest transient observation goes first.
                candidates = list(self._normal)
                droppable = [e for e in candidates if e.priority < int(Priority.HIGH)]
                if droppable:
                    victim = min(droppable, key=lambda e: (e.priority, e.timestamp_mono))
                    self._normal.remove(victim)
                    dropped.append(victim.event_id)
                    self._record_drop(victim, "lowest_priority_transient_observation")
                elif event.priority < int(Priority.HIGH) and event.kind not in NO_SILENT_DROP_EVENT_KINDS:
                    self._record_drop(event, "incoming_low_priority_overload")
                    self._mark_seen(event.event_id)
                    return EnqueueReceipt(False, "incoming_low_priority_overload", event.event_id, tuple(dropped + [event.event_id]))
                else:
                    # Important normal events are explicitly backpressured, not silently lost.
                    raise BufferError(f"ordinary event queue full; caller must apply backpressure for {event.event_id}")

            self._normal.append(event)
            self._mark_seen(event.event_id)
            return EnqueueReceipt(True, "accepted", event.event_id, tuple(dropped))

    def drain(self, limit: int = 100, now_mono: float | None = None) -> list[Event]:
        events, _dropped = self.drain_with_drops(limit=limit, now_mono=now_mono)
        return events

    def drain_with_drops(self, limit: int = 100, now_mono: float | None = None) -> tuple[list[Event], tuple[str, ...]]:
        if limit < 1:
            return [], ()
        if now_mono is not None and (not isinstance(now_mono, (int, float)) or not math.isfinite(now_mono)):
            raise ValueError("now_mono must be finite")
        out: list[Event] = []
        dropped: list[str] = []
        with self._lock:
            if now_mono is not None:
                kept = deque()
                while self._normal:
                    event = self._normal.popleft()
                    if event.expires_at_mono is not None and event.expires_at_mono <= now_mono:
                        dropped.append(event.event_id)
                        self._record_drop(event, "expired_at_dequeue")
                    else:
                        kept.append(event)
                self._normal = kept
            # Protected controls are not expired/dropped by this path.
            while self._control and len(out) < limit:
                out.append(self._control.popleft())
            while self._normal and len(out) < limit:
                out.append(self._normal.popleft())
        return out, tuple(dropped)

    def consume_latches(self) -> ControlLatches:
        """Return and clear edge-triggered latch contents; degraded remains until reset."""
        with self._lock:
            result = ControlLatches(
                safety_stop=self.latches.safety_stop,
                user_stop=self.latches.user_stop,
                muted=self.latches.muted,
                cancelled_intent_ids=set(self.latches.cancelled_intent_ids),
                quiet_scope=self.latches.quiet_scope, quiet_changed=self.latches.quiet_changed,
                cancel_all=self.latches.cancel_all, degraded=self.latches.degraded,
                user_speaking=self.latches.user_speaking, speech_started=self.latches.speech_started,
                cancelled_task_ids=set(self.latches.cancelled_task_ids), cancel_all_tasks=self.latches.cancel_all_tasks,
                paused_task_ids=set(self.latches.paused_task_ids), pause_all_tasks=self.latches.pause_all_tasks,
            )
            self.latches.safety_stop = False
            self.latches.user_stop = False
            self.latches.muted = None
            self.latches.cancelled_intent_ids.clear()
            self.latches.quiet_scope = None
            self.latches.quiet_changed = False
            self.latches.cancel_all = False
            # user_speaking is a persistent level; speech_started is an edge latch that
            # remains set until the main loop has had a chance to observe the barge-in.
            self.latches.speech_started = False
            self.latches.cancelled_task_ids.clear()
            self.latches.cancel_all_tasks = False
            self.latches.paused_task_ids.clear()
            self.latches.pause_all_tasks = False
            return result

    def peek_latches(self) -> ControlLatches:
        """Thread-safe snapshot used by action dispatch before the next main-loop tick."""
        with self._lock:
            return ControlLatches(
                safety_stop=self.latches.safety_stop, user_stop=self.latches.user_stop,
                muted=self.latches.muted, cancelled_intent_ids=set(self.latches.cancelled_intent_ids),
                quiet_scope=self.latches.quiet_scope, quiet_changed=self.latches.quiet_changed,
                cancel_all=self.latches.cancel_all, degraded=self.latches.degraded,
                user_speaking=self.latches.user_speaking, speech_started=self.latches.speech_started,
                cancelled_task_ids=set(self.latches.cancelled_task_ids), cancel_all_tasks=self.latches.cancel_all_tasks,
                paused_task_ids=set(self.latches.paused_task_ids), pause_all_tasks=self.latches.pause_all_tasks,
            )

    def reset_degraded_after_health_check(self) -> None:
        with self._lock:
            self.latches.degraded = False

    def dropped_summary(self) -> dict[str, int]:
        with self._lock:
            return dict(self.drop_counts)
