"""Structured preflight and field-test reporting for v0.24 deployments.

The runner never moves hardware or emits sound unless the host injects an explicit
probe. A simulated preflight is labelled SIMULATED and cannot be represented as a field
validation pass. Physical-device checks are opt-in and depend on host-supplied probes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import inspect
import platform
import sys
import time
from typing import Any, Awaitable, Callable, Mapping


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    name: str
    status: str
    detail: str
    duration_s: float = 0.0
    mode: str = "SIMULATED"
    physical_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "duration_s": round(max(0.0, self.duration_s), 6), "mode": self.mode,
                "physical_evidence": self.physical_evidence}


@dataclass(frozen=True, slots=True)
class FieldValidationReport:
    schema_version: str
    created_at: float
    mode: str
    checks: tuple[ValidationCheck, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.status in {"PASS", "SKIP"} for c in self.checks)

    @property
    def field_validated(self) -> bool:
        has_physical_probe = any(c.name.startswith("probe:") and c.mode == "FIELD" and c.physical_evidence for c in self.checks)
        return self.mode == "FIELD" and self.passed and has_physical_probe

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "created_at": self.created_at,
                "mode": self.mode, "passed": self.passed, "field_validated": self.field_validated,
                "checks": [c.to_dict() for c in self.checks], "metadata": dict(self.metadata)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)


Probe = Callable[[], Awaitable[bool | Mapping[str, Any]] | bool | Mapping[str, Any]]


class FieldValidationRunner:
    """Run non-destructive readiness checks plus caller-provided explicit probes."""
    def __init__(self, engine: Any, companion: Any | None = None, *,
                 voice_pipeline: Any | None = None, body_coordinator: Any | None = None,
                 llm_runtime: Any | None = None, probes: Mapping[str, Probe] | None = None, clock=time.time):
        self.engine, self.companion = engine, companion
        self.voice_pipeline, self.body_coordinator = voice_pipeline, body_coordinator
        self.llm_runtime = llm_runtime
        self.probes = dict(probes or {})
        self.clock = clock

    async def _check(self, name: str, fn: Callable[[], Any], *, mode: str = "SIMULATED") -> ValidationCheck:
        started = time.monotonic()
        physical_evidence = False
        try:
            result = fn()
            if inspect.isawaitable(result):
                result = await result
            if callable(getattr(result, "to_dict", None)):
                result = result.to_dict()
            if isinstance(result, Mapping):
                physical_evidence = result.get("physical_evidence") is True
                if "reachable" in result:
                    status = "PASS" if result.get("reachable") is True else ("SKIP" if result.get("reachable") is None else "FAIL")
                    detail = str(result.get("note", result.get("error_code", status)))[:300]
                else:
                    status = str(result.get("status", "PASS" if result.get("ok") else "FAIL"))
                    detail = str(result.get("detail", result.get("reason", status)))[:300]
            else:
                status = "PASS" if bool(result) else "FAIL"
                detail = "check succeeded" if result else "check failed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status, detail = "FAIL", f"{type(exc).__name__}"
        return ValidationCheck(name, status, detail, max(0.0, time.monotonic() - started), mode, physical_evidence)

    async def run(self, *, field_mode: bool = False, perform_provider_probe: bool = False) -> FieldValidationReport:
        mode = "FIELD" if field_mode else "SIMULATED"
        checks: list[ValidationCheck] = []
        engine = self.engine
        checks.append(await self._check("engine_present", lambda: engine is not None))
        checks.append(await self._check("safety_supervisor_present", lambda: getattr(engine, "safety", None) is not None))
        checks.append(await self._check("llm_provider_configured", lambda: getattr(engine, "llm_gateway", None) is not None))
        repo = getattr(engine, "memory_repository", None) if engine is not None else None
        def probe_memory_store():
            if repo is None or not callable(getattr(repo, "list_memories", None)):
                return False
            rows = repo.list_memories(limit=1)
            return isinstance(rows, list)
        checks.append(await self._check("memory_repository_readable", probe_memory_store))
        checks.append(await self._check("generation_control_available", lambda: callable(getattr(engine, "current_generation", None)) and callable(getattr(engine, "invalidate_interactions", None))))
        if self.companion is not None:
            metrics = getattr(self.companion, "metrics", None)
            checks.append(await self._check("companion_metrics_available", lambda: callable(getattr(metrics, "snapshot", None))))
            checks.append(await self._check("companion_session_clear_available", lambda: callable(getattr(self.companion, "clear_session", None))))
        else:
            checks.append(ValidationCheck("companion_service", "SKIP", "not provided", 0.0, mode))
        if self.voice_pipeline is not None:
            checks.append(await self._check("voice_adapters_present", lambda: all(
                callable(getattr(self.voice_pipeline, attr, None)) for attr in ("process_audio", "cancel", "aclose"))))
        else:
            checks.append(ValidationCheck("voice_pipeline", "SKIP", "not connected; no microphone was opened", 0.0, mode))
        body_enabled = bool(getattr(engine, "body_enabled", False)) if engine is not None else False
        body_controller = getattr(engine, "body_controller", None) if engine is not None else None
        if body_enabled:
            checks.append(await self._check("body_driver_present", lambda: body_controller is not None))
            health = getattr(body_controller, "health_check", None) if body_controller is not None else None
            if callable(health):
                checks.append(await self._check("body_driver_health", health, mode=mode))
            else:
                checks.append(ValidationCheck("body_driver_health", "SKIP", "driver exposes no health probe", 0.0, mode))
        else:
            checks.append(ValidationCheck("body_driver", "SKIP", "body execution disabled; no physical motion attempted", 0.0, mode))

        for name, probe in sorted(self.probes.items()):
            if not name or len(name) > 100:
                checks.append(ValidationCheck("invalid_probe_name", "FAIL", "probe name must contain 1..100 characters", 0.0, mode))
                continue
            checks.append(await self._check("probe:" + name, probe, mode=mode if field_mode else "SIMULATED"))

        provider = getattr(engine, "llm_gateway", None) if engine is not None else None
        if provider is not None and perform_provider_probe:
            if callable(getattr(self.llm_runtime, "probe", None)):
                checks.append(await self._check("provider_inference_probe", lambda: self.llm_runtime.probe(perform_inference=True), mode=mode))
            else:
                checks.append(ValidationCheck("provider_inference_probe", "SKIP", "LLM gateway is configured, but no explicit runtime probe was supplied", 0.0, mode))
        else:
            checks.append(ValidationCheck("provider_reachability", "SKIP", "no live inference probe was requested", 0.0, mode))

        metadata = {"python": sys.version.split()[0], "platform": platform.platform(),
                    "engine_replay_mode": bool(getattr(engine, "replay_mode", False)) if engine is not None else None,
                    "raw_user_text_recorded": False,
                    "field_mode_notice": "FIELD mode is still only as real as its injected hardware/provider probes."}
        return FieldValidationReport("1.0", float(self.clock()), mode, tuple(checks), metadata)
