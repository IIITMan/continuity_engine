"""Coordinator for optional LIFE_LAYER organs; safety and existing managers remain authoritative."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Mapping
from ..core.models import DriveState, Intent, IntentCandidate, WorldFact
from .attachment import Attachment, ReunionResponse
from .breath import BreathModel
from .drive_field import DriveField, FieldSnapshot
from .life_stream import LifeStream
from .habituation import HabituationTracker
from .temperament import Temperament
from .will import WillArbiter

class LifeLayer:
    def __init__(self, config: Mapping[str, Any] | None = None, *, now_mono: float = 0.0, now_wall: float = 0.0):
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", True))
        field_cfg = cfg.get("field", {})
        self.field_enabled = bool(field_cfg.get("enabled", True))
        will_cfg = cfg.get("will", {})
        attachment_cfg = cfg.get("attachment", {})
        stream_cfg = cfg.get("life_stream", {})
        temperament_cfg = cfg.get("temperament", {})
        breath_cfg = cfg.get("breath", {})
        habituation_cfg = cfg.get("habituation", {})
        self.field = DriveField(security_wave=str(field_cfg.get("security_wave", "safety")),
                                low_activation=float(field_cfg.get("low_activation", .45)),
                                sleep_security_threshold=float(field_cfg.get("sleep_security_threshold", .45)),
                                sleep_struggle_cap=float(field_cfg.get("sleep_struggle_cap", .30)))
        self.will = WillArbiter(tick_s=float(will_cfg.get("tick_s", 5)),
                                low_tension_threshold=float(will_cfg.get("low_tension_threshold", .45)),
                                useless_probability=float(will_cfg.get("useless_probability", .08)),
                                attempt_max_retry=int(will_cfg.get("attempt_max_retry", 3)),
                                action_waves_excluded=tuple(will_cfg.get("action_waves_excluded", ["safety", "security"])),
                                enabled=bool(will_cfg.get("enabled", True)))
        self.attachment = Attachment(trust=float(attachment_cfg.get("initial_trust", 20)),
                                     imprint_days=int(attachment_cfg.get("imprint_days", 14)),
                                     separation_k=float(attachment_cfg.get("separation_k", .0009)),
                                     separation_cap=float(attachment_cfg.get("separation_cap", .8)),
                                     brief_s=float(attachment_cfg.get("brief_s", 1800)),
                                     long_away_s=float(attachment_cfg.get("long_away_s", 14400)),
                                     confidence_min=float(attachment_cfg.get("confidence_min", .85)),
                                     enabled=bool(attachment_cfg.get("enabled", True)))
        self.life_stream = LifeStream(max_entries_per_day=int(stream_cfg.get("max_entries_per_day", 500)),
                                      retention_days=int(stream_cfg.get("retention_days", 7)),
                                      enabled=bool(stream_cfg.get("enabled", True)), seed=str(stream_cfg.get("seed", "continuity")))
        self.temperament = Temperament(traits=temperament_cfg.get("initial_traits"),
                                       baseline=float(temperament_cfg.get("baseline", .5)),
                                       max_offset=float(temperament_cfg.get("max_offset", .3)),
                                       event_step=float(temperament_cfg.get("event_step", .08)),
                                       enabled=bool(temperament_cfg.get("enabled", True)), birth_wall=now_wall)
        self.habituation = HabituationTracker(enabled=bool(habituation_cfg.get("enabled", True)),
                                              repeat_scale=float(habituation_cfg.get("repeat_scale", .25)),
                                              max_event_types=int(habituation_cfg.get("max_event_types", 32)))
        self.breath = BreathModel(base_bpm=float(breath_cfg.get("base_bpm", 12)), max_bpm=float(breath_cfg.get("max_bpm", 24)),
                                  sleep_bpm=float(breath_cfg.get("sleep_bpm", 9)), min_depth=float(breath_cfg.get("min_depth", .4)),
                                  max_depth=float(breath_cfg.get("max_depth", 1)), arousal_tau_s=float(breath_cfg.get("arousal_tau_s", 4)),
                                  burst_tau_s=float(breath_cfg.get("burst_tau_s", 20)), enabled=bool(breath_cfg.get("enabled", True)))
        self.last_field: FieldSnapshot | None = None
        self._last_presence_signature: tuple[str, bool] | None = None
        self._previously_sleeping = False
        self.last_reunion: dict[str, Any] | None = None
        self._pending_reunion_pulse: dict[str, float] | None = None
        self.last_tick_mono = now_mono
        self._last_day = datetime.fromtimestamp(now_wall, timezone.utc).date().isoformat()

    def interrupt(self, reason: str, *, cancelled: bool, now_mono: float) -> None:
        if self.enabled:
            self.will.interrupt(reason, cancelled=cancelled, now_mono=now_mono)

    def set_sleeping(self, sleeping: bool, *, now_wall: float | None = None) -> None:
        if not self.enabled:
            return
        now_wall = 0.0 if now_wall is None else float(now_wall)
        entering_sleep = bool(sleeping) and not self._previously_sleeping
        self.breath.set_sleeping(sleeping)
        self._previously_sleeping = bool(sleeping)
        if entering_sleep and now_wall > 0:
            day = datetime.fromtimestamp(now_wall, timezone.utc).date().isoformat()
            self.life_stream.consolidate_night(day)

    def startle(self, event_type: str, intensity: float, *, now_wall: float) -> bool:
        """Apply habituation to expression only; raw event and safety classification stay intact."""
        if not self.enabled:
            return False
        params = self.temperament.action_params()
        sensitivity_adjusted = max(0.0, min(1.0, float(intensity) * float(params["startle_expression_scale"])))
        expressive_intensity, familiar = self.habituation.expressive_intensity(event_type, sensitivity_adjusted)
        self.breath.shock(expressive_intensity, now_wall=now_wall)
        return familiar

    def consume_reunion_pulse(self) -> dict[str, float] | None:
        """Return a newly generated pulse once; restored history never replays it."""
        pulse = self._pending_reunion_pulse
        self._pending_reunion_pulse = None
        return dict(pulse) if pulse is not None else None

    def tick(self, *, dt: float, now_mono: float, now_wall: float, drives: Any,
             world: Mapping[str, WorldFact], affect: Mapping[str, Any], current_intent: Intent | None,
             quiet_scope: str | None, user_speaking: bool, safety_stopped: bool) -> list[IntentCandidate]:
        if not self.enabled: return []
        day = datetime.fromtimestamp(now_wall, timezone.utc).date().isoformat()
        # If the host missed an explicit sleep transition, crossing a UTC day boundary
        # still consolidates the prior day exactly once. Sleep is never inferred from silence.
        if day != self._last_day:
            previous = self._last_day
            if previous in self.life_stream.daily and previous not in self.life_stream.consolidated:
                self.life_stream.consolidate_night(previous)
            self._last_day = day
        owner = world.get("owner_present")
        if owner is not None:
            previous_status = self._last_presence_signature
            signature = (owner.status.value, bool(owner.value) if isinstance(owner.value, bool) else False)
            response = self.attachment.observe_presence(owner, dt=dt, now_wall=now_wall)
            if signature != previous_status and owner.status.value == "CONFIRMED" and isinstance(owner.value, bool) and owner.confidence >= self.attachment.confidence_min:
                self._last_presence_signature = signature
            if response is not None:
                self.last_reunion = {"category": response.category, "reason_code": response.reason_code,
                                     "trust_gain": response.trust_gain, "mood_delta": response.mood_delta,
                                     "attachment_impulse": response.attachment_impulse, "timestamp_wall": now_wall}
                self._pending_reunion_pulse = {"valence_delta": response.mood_delta,
                                               "attachment_impulse": response.attachment_impulse}
                if "attachment" in drives.states:
                    drives.impulse("attachment", response.attachment_impulse)
                self.life_stream.record(day=day, kind="reunion", summary_key="confirmed_reunion", source_id=f"reunion:{int(now_wall)}",
                                        timestamp_wall=now_wall, metadata={"category": response.category})
        # Attachment is a signal only: it cannot make user absence certain or authorize output.
        if "attachment" in drives.states:
            attachment_target = self.attachment.separation_level if self.attachment.enabled and self.attachment.confirmed_absent else 0.0
            drives.set_target("attachment", attachment_target)
        arousal = float(affect.get("arousal", .18))
        breath_reactivity = float(self.temperament.action_params()["arousal_response_scale"])
        self.breath.update(dt=dt, arousal=max(0.0, min(1.0, arousal * breath_reactivity)), now_wall=now_wall)
        self.last_field = self.field.snapshot(drives.snapshot(), sleeping=self.breath.sleeping) if self.field_enabled else None
        candidates = self.will.propose(self.last_field, dt=dt, now_mono=now_mono, current_intent=current_intent,
                                       quiet_scope=quiet_scope, user_speaking=user_speaking, safety_stopped=safety_stopped) if self.last_field is not None else []
        self.life_stream.prune(day)
        self.last_tick_mono = now_mono
        return candidates

    def context_for_today(self, now_wall: float) -> list[dict[str, Any]]:
        day = datetime.fromtimestamp(now_wall, timezone.utc).date().isoformat()
        return self.life_stream.recent_context(before_day=day, limit=3)

    def snapshot(self, now_wall: float | None = None) -> dict[str, Any]:
        result = {"schema_version": 1, "enabled": self.enabled, "will": self.will.snapshot(),
                  "attachment": self.attachment.snapshot(), "life_stream": self.life_stream.snapshot(),
                  "temperament": self.temperament.snapshot(), "breath": self.breath.snapshot(),
                  "habituation": self.habituation.snapshot(),
                  "last_reunion": self.last_reunion, "last_tick_mono": 0.0, "last_day": self._last_day,
                  "life_context": self.context_for_today(now_wall) if now_wall is not None else []}
        return result

    def restore(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping): return
        organs = (
            ("will", self.will.restore, lambda: setattr(self, "will", WillArbiter(enabled=self.will.enabled))),
            ("attachment", self.attachment.restore, lambda: setattr(self, "attachment", Attachment(enabled=self.attachment.enabled))),
            ("life_stream", self.life_stream.restore, lambda: setattr(self, "life_stream", LifeStream(enabled=self.life_stream.enabled))),
            ("temperament", self.temperament.restore, lambda: setattr(self, "temperament", Temperament(enabled=self.temperament.enabled))),
            ("breath", self.breath.restore, lambda: setattr(self, "breath", BreathModel(enabled=self.breath.enabled))),
            ("habituation", self.habituation.restore, lambda: setattr(self, "habituation", HabituationTracker(enabled=self.habituation.enabled))),
        )
        for key, restore, reset in organs:
            if isinstance(data.get(key), Mapping):
                try:
                    restore(data[key])
                except (ValueError, TypeError, KeyError):
                    reset()  # one corrupt organ must not erase valid sibling state.
        reunion = data.get("last_reunion")
        self.last_reunion = dict(reunion) if isinstance(reunion, Mapping) else None
        self._pending_reunion_pulse = None  # never replay event pulses after restoring a snapshot
        saved_day = data.get("last_day")
        if isinstance(saved_day, str):
            try:
                self._last_day = datetime.fromisoformat(saved_day).date().isoformat()
            except ValueError:
                pass
