"""Real-provider acceptance drill: compatibility, long conversation, failure recovery.

This module is a host-driven harness, not an automated CI test. It performs *real*
network calls through an injected environment (see :class:`DrillEnvironment`) and
verifies, for a specific vendor/model, that the provider:

* honors the engine's per-purpose JSON output contracts;
* can sustain a 50-100 turn conversation while recalling memory and continuing context;
* tolerates timeouts and rate limits through controlled retry without replaying a
  request that may have produced an external side effect;
* opens a circuit breaker during an outage, fails fast, and recovers afterwards;
* survives cancellation without leaking tasks;
* resumes after a process restart using persisted context.

Reports contain statuses, timings, counters and boolean expectation results. Raw
prompts and raw model responses are never written into a report.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import tracemalloc
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ..llm.llm_gateway import LLMRequest, LLMStatus
from ..llm.provider_errors import (
    ProviderAuthError, ProviderConnectError, ProviderRateLimitError, ProviderServerError,
    ProviderTimeoutError,
)
from ..llm.reliability import CircuitBreaker, ReliabilityMetrics, ReliableLLMAdapter, RetryPolicy


# --------------------------------------------------------------------------- #
# Fault injection
# --------------------------------------------------------------------------- #
FAULT_RATE_LIMIT = "RATE_LIMIT"
FAULT_SERVER = "SERVER"
FAULT_TIMEOUT = "TIMEOUT"
FAULT_CONNECT = "CONNECT"
FAULT_AUTH = "AUTH"
_FAULT_KINDS = {FAULT_RATE_LIMIT, FAULT_SERVER, FAULT_TIMEOUT, FAULT_CONNECT, FAULT_AUTH}


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """One deterministic client-side simulated provider fault."""

    kind: str
    count: int = 1
    retry_after_s: float | None = None
    status_code: int = 503
    delay_s: float = 0.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _FAULT_KINDS:
            raise ValueError(f"fault kind must be one of {sorted(_FAULT_KINDS)}")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or not 1 <= self.count <= 100:
            raise ValueError("count must be an integer in [1,100]")
        if self.retry_after_s is not None and (not isinstance(self.retry_after_s, (int, float))
                                               or isinstance(self.retry_after_s, bool)
                                               or not math.isfinite(float(self.retry_after_s))
                                               or not 0 <= float(self.retry_after_s) <= 600):
            raise ValueError("retry_after_s must be finite and in [0,600]")
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool) or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be a valid HTTP status")
        if not isinstance(self.delay_s, (int, float)) or isinstance(self.delay_s, bool) or not math.isfinite(float(self.delay_s)) or not 0 <= float(self.delay_s) <= 600:
            raise ValueError("delay_s must be finite and in [0,600]")


def expand_faults(specs: Sequence[FaultSpec]) -> list[FaultSpec]:
    out: list[FaultSpec] = []
    for spec in specs:
        for _ in range(spec.count):
            out.append(FaultSpec(spec.kind, count=1, retry_after_s=spec.retry_after_s,
                                 status_code=spec.status_code, delay_s=spec.delay_s, label=spec.label))
    return out


class FaultInjectingAdapter:
    """Wrap a real provider adapter and inject a deterministic FIFO fault schedule.

    Simulating a fault client-side makes the drill reproducible while the satisfied
    schedule still reaches the real network path, so latency and success are measured
    against the live provider. ``outage`` simulates a sustained provider outage.
    """

    is_replay_adapter = False

    def __init__(self, delegate: Any, *, faults: Sequence[FaultSpec] = (), outage: bool = False,
                 clock=time.monotonic) -> None:
        self.delegate = delegate
        self.clock = clock
        self._faults = expand_faults(faults)
        self._outage = bool(outage)
        self.calls = 0
        self.injected: list[str] = []

    @property
    def model(self) -> str | None:
        return getattr(self.delegate, "model", None)

    def set_faults(self, faults: Sequence[FaultSpec]) -> None:
        self._faults = expand_faults(faults)

    def clear_faults(self) -> None:
        self._faults = []

    def set_outage(self, outage: bool) -> None:
        self._outage = bool(outage)

    @property
    def pending_faults(self) -> int:
        return len(self._faults)

    def describe(self) -> dict[str, Any]:
        return type(self.delegate).__name__

    def snapshot(self) -> dict[str, Any]:
        return {"calls": self.calls, "injected": list(self.injected),
                "outage": self._outage, "pending_faults": len(self._faults)}

    async def generate(self, request: LLMRequest) -> Mapping[str, Any]:
        self.calls += 1
        if self._outage:
            self.injected.append("OUTAGE")
            raise ProviderServerError("injected_provider_outage", status_code=503)
        if self._faults:
            spec = self._faults.pop(0)
            self.injected.append(spec.kind)
            if spec.delay_s:
                await asyncio.sleep(spec.delay_s)
            if spec.kind == FAULT_RATE_LIMIT:
                raise ProviderRateLimitError("injected_rate_limit", retry_after_s=spec.retry_after_s,
                                             status_code=429)
            if spec.kind == FAULT_TIMEOUT:
                raise ProviderTimeoutError("injected_timeout")
            if spec.kind == FAULT_CONNECT:
                raise ProviderConnectError("injected_connect_failure")
            if spec.kind == FAULT_AUTH:
                raise ProviderAuthError("injected_auth_failure", status_code=401)
            raise ProviderServerError("injected_server_error", status_code=spec.status_code)
        return await self.delegate.generate(request)

    async def aclose(self) -> None:
        closer = getattr(self.delegate, "aclose", None)
        if closer is not None:
            await closer()


# --------------------------------------------------------------------------- #
# Resource sampling
# --------------------------------------------------------------------------- #
def _rss_bytes() -> int | None:
    try:  # optional dependency
        import psutil  # type: ignore
        return int(psutil.Process().memory_info().rss)
    except Exception:
        pass
    try:
        import resource  # POSIX only
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        pass
    try:  # Windows: K32GetProcessMemoryInfo (kernel32) with a psapi.dll fallback
        import ctypes
        from ctypes import wintypes

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel32.GetCurrentProcess()
        for library, symbol in (("kernel32", "K32GetProcessMemoryInfo"), ("psapi", "GetProcessMemoryInfo")):
            try:
                func = getattr(getattr(ctypes.windll, library), symbol)
            except (AttributeError, OSError):
                continue
            # Without argtypes/restype ctypes passes an int instead of a pointer on
            # 64-bit Windows and the call silently fails with return code 0.
            func.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
            func.restype = wintypes.BOOL
            if func(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
    except Exception:
        pass
    return None


class ResourceSampler:
    """Sample process memory, thread and task counts during a drill.

    Only aggregate numbers are reported; no payload data is inspected.
    """

    def __init__(self, *, interval_s: float = 0.25, clock=time.monotonic, loop_task_counter=None) -> None:
        if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)) or not math.isfinite(float(interval_s)) or not 0.05 <= float(interval_s) <= 60:
            raise ValueError("interval_s must be finite and in [0.05,60]")
        self.interval_s = float(interval_s)
        self.clock = clock
        self._loop_task_counter = loop_task_counter or (lambda: len(asyncio.all_tasks()))
        self._samples: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._peak_traced = 0

    def sample_once(self) -> dict[str, Any]:
        row: dict[str, Any] = {"t": round(float(self.clock()), 6), "rss_bytes": _rss_bytes(),
                               "threads": len(threading.enumerate())}
        try:
            row["asyncio_tasks"] = int(self._loop_task_counter())
        except Exception:
            row["asyncio_tasks"] = None
        if tracemalloc.is_tracing():
            current, peak = tracemalloc.get_traced_memory()
            row["py_traced_bytes"] = int(current)
            self._peak_traced = max(self._peak_traced, int(peak))
            row["py_traced_peak_bytes"] = self._peak_traced
        self._samples.append(row)
        return row

    async def start(self) -> None:
        self.sample_once()
        self._task = asyncio.create_task(self._run(), name="provider-drill-resource-sampler")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                self.sample_once()

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.sample_once()

    def _series(self, key: str) -> list[float]:
        return [float(row[key]) for row in self._samples if isinstance(row.get(key), (int, float))]

    def summary(self) -> dict[str, Any]:
        def stats(key: str) -> dict[str, Any] | None:
            values = self._series(key)
            if not values:
                return None
            return {"min": min(values), "max": max(values), "mean": sum(values) / len(values), "last": values[-1]}

        return {"sample_count": len(self._samples),
                "rss_bytes": stats("rss_bytes"),
                "py_traced_bytes": stats("py_traced_bytes"),
                "py_traced_peak_bytes": self._peak_traced or None,
                "threads": stats("threads"),
                "asyncio_tasks": stats("asyncio_tasks"),
                "note": "RSS is null when neither psutil nor a platform memory API is available."}


# --------------------------------------------------------------------------- #
# Drill configuration and session contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DrillConfig:
    turns: int = 60
    max_report_turns: int = 200
    probe_timeout_s: float = 60.0
    compat_timeout_s: float = 45.0
    #: Independent samples per output-contract probe. A single sample failing a documented
    #: bound is probabilistic model behavior; the honest metric is the observed violation
    #: rate, reported alongside a pass that requires at least one valid sample.
    compat_attempts: int = 3
    expect_thinking_disabled: bool = False
    measure_python_memory: bool = True
    run_compatibility: bool = True
    run_conversation: bool = True
    run_transport_drills: bool = True
    run_cancellation: bool = True
    run_restart: bool = True
    run_outage_recovery: bool = True
    retry_policy: RetryPolicy = field(default_factory=lambda: RetryPolicy(
        max_attempts=3, base_delay_s=0.4, max_delay_s=4.0, max_total_retry_wait_s=10.0))
    circuit_failure_threshold: int = 3
    circuit_recovery_timeout_s: float = 2.0
    deadline_timeout_s: float = 1.0
    cancellation_settle_s: float = 0.8
    #: An injected slow fault keeps a request deterministically in flight while the drill
    #: cancels it, so the cancellation assertion does not race a fast provider failure.
    cancellation_inflight_s: float = 6.0
    #: Bounded attempts for a single-shot assertion that can be disturbed by a real
    #: provider incident (a transient connectivity failure is not a harness defect).
    restart_attempts: int = 3
    outage_recovery_attempts: int = 3
    #: Bounded attempts for a labelled conversation probe (memory recall / context
    #: continuation), which asserts a capability rather than provider availability.
    probe_turn_attempts: int = 3
    #: End-to-end readiness check before the scenarios, so a run that starts during a
    #: provider outage is labelled as such instead of looking like an engine defect.
    run_preflight: bool = True
    memory_statement: str = "请记住：我的猫叫雪球。"
    memory_question: str = "我的猫叫什么名字？"
    memory_expected_tokens: tuple[str, ...] = ("雪球",)
    memory_expected_turn_index: int = 4
    context_statement: str = "我们这周的目标是准备马拉松，请先帮我理一下要怎么安排。"
    context_question: str = "我们这周的目标是什么？"
    context_expected_tokens: tuple[str, ...] = ("马拉松",)
    context_expected_turn_index: int = 12
    restart_check_tokens: tuple[str, ...] = ("雪球",)

    def __post_init__(self) -> None:
        if isinstance(self.turns, bool) or not isinstance(self.turns, int) or not 10 <= self.turns <= 200:
            raise ValueError("turns must be an integer in [10,200]")
        if isinstance(self.compat_attempts, bool) or not isinstance(self.compat_attempts, int) or not 1 <= self.compat_attempts <= 10:
            raise ValueError("compat_attempts must be an integer in [1,10]")
        for name in ("memory_expected_turn_index", "context_expected_turn_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < self.turns:
                raise ValueError(f"{name} must be an integer in [0,turns)")
        if self.memory_expected_turn_index >= self.context_expected_turn_index:
            raise ValueError("memory_expected_turn_index must precede context_expected_turn_index")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise ValueError("retry_policy must be a RetryPolicy")
        if isinstance(self.circuit_failure_threshold, bool) or not isinstance(self.circuit_failure_threshold, int) or not 1 <= self.circuit_failure_threshold <= 20:
            raise ValueError("circuit_failure_threshold must be an integer in [1,20]")
        for name in ("probe_timeout_s", "compat_timeout_s", "deadline_timeout_s",
                     "cancellation_settle_s", "circuit_recovery_timeout_s",
                     "cancellation_inflight_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("restart_attempts", "outage_recovery_attempts", "probe_turn_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
                raise ValueError(f"{name} must be an integer in [1,10]")


@dataclass(slots=True)
class DrillSession:
    """A live engine + companion bound to one provider connection."""

    label: str
    engine: Any
    companion: Any
    runtime: Any | None = None
    fault_adapter: FaultInjectingAdapter | None = None
    memory_repository: Any | None = None
    session_store: Any | None = None

    def close(self) -> None:
        closer = getattr(self.engine, "close", None)
        if callable(closer):
            closer()
        for resource in (self.memory_repository, self.session_store):
            close = getattr(resource, "close", None)
            if callable(close):
                close()


class DrillEnvironment(Protocol):
    """Host-supplied real-provider plumbing for the drill."""

    def describe(self) -> Mapping[str, Any]: ...

    async def create_session(self, *, label: str) -> DrillSession: ...

    async def close_session(self, session: DrillSession) -> None: ...

    async def create_fault_adapter(self, *, label: str, faults: Sequence[FaultSpec] = (),
                                   outage: bool = False) -> FaultInjectingAdapter: ...

    async def close_fault_adapter(self, adapter: FaultInjectingAdapter) -> None: ...


# --------------------------------------------------------------------------- #
# Authored conversation script
# --------------------------------------------------------------------------- #
_CONVERSATION_BASE: tuple[tuple[str, dict[str, Any]], ...] = (
    ("你好呀，今天过得怎么样？", {}),
    ("我最近在整理书桌，有点想把它变得更适合工作。", {}),
    ("我现在有点累，但我还想把手头的报告写完，你能鼓励我一句吗？", {"min_chars": 10}),
    ("帮我看看明天适不适合去跑步？", {"required_any": ("天气", "无法", "不清楚", "没有", "城市", "地区", "查询"),
                                "forbidden_any": ("°C", "晴转", "多云转")}),
    ("你觉得专注工作和及时休息哪个更重要？", {}),
    ("给我讲讲为什么人会习惯性地拖延。", {"min_chars": 20}),
    ("如果我只想每天花二十分钟运动，你会怎么安排？", {"min_chars": 15}),
    ("我今天和同事之间发生了一点小摩擦，心里有点不舒服。", {}),
    ("我是不是应该主动和他聊聊？", {}),
    ("谢谢你听我说这些。", {}),
    ("顺便问一下，你还记得我喜欢什么吗？", {}),
    ("我们换个话题，聊聊怎么让房间更安静一些。", {}),
    ("我想在晚上睡前做一点放松的事情，有什么建议？", {}),
    ("如果我在夜里突然醒了睡不着，应该怎么办？", {}),
)

#: Companion statuses that mean "the turn produced a usable answer".
_CONVERSATION_OK_STATUSES = frozenset({"COMPLETED", "MEMORY_SAVED"})
#: A disclosed local fallback is the *correct* behaviour during a provider outage, so it
#: is counted separately from a hard failure (a turn that produced no answer at all).
_CONVERSATION_SOFT_STATUSES = frozenset({"DEGRADED"})

_CONVERSATION_FILLER: tuple[tuple[str, dict[str, Any]], ...] = (
    ("今天天气好像变凉了，你有没有这种感觉？", {}),
    ("我在考虑换一种更规律的作息。", {}),
    ("如果我坚持两周早起，会有什么变化？", {}),
    ("有时候我觉得自己效率很低。", {}),
    ("你更喜欢安静地陪着，还是多说几句话？", {}),
    ("我最近读书有点慢，有什么办法吗？", {}),
    ("帮我用一个比喻形容一下坚持这件事。", {"min_chars": 10}),
    ("我打算这周末去公园走走。", {}),
    ("你会在意我说话的语气吗？", {}),
    ("如果我说的事情你不太确定，你会怎么回应？", {}),
    ("我一直想学一门乐器，但总是拖着。", {}),
    ("你觉得记录待办清单真的有用吗？", {}),
    ("我今天喝了很多咖啡，是不是不太好？", {}),
    ("陪我说说轻松的日常吧。", {}),
)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DrillReport:
    schema_version: str
    run_id: str
    started_at: float
    completed_at: float
    provider: Mapping[str, Any]
    config: Mapping[str, Any]
    compatibility: tuple[Mapping[str, Any], ...]
    conversation: Mapping[str, Any]
    transport_drills: tuple[Mapping[str, Any], ...]
    cancellation: Mapping[str, Any]
    restart: Mapping[str, Any]
    outage_recovery: Mapping[str, Any]
    reliability: Mapping[str, Any]
    companion_metrics: Mapping[str, Any]
    resources: Mapping[str, Any]
    verdicts: Mapping[str, bool]
    token_usage: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return bool(self.verdicts) and all(self.verdicts.values())

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "run_id": self.run_id,
                "started_at": self.started_at, "completed_at": self.completed_at,
                "duration_s": round(max(0.0, self.completed_at - self.started_at), 3),
                "provider": dict(self.provider), "config": dict(self.config),
                "compatibility": [dict(c) for c in self.compatibility],
                "conversation": dict(self.conversation),
                "transport_drills": [dict(d) for d in self.transport_drills],
                "cancellation": dict(self.cancellation), "restart": dict(self.restart),
                "outage_recovery": dict(self.outage_recovery),
                "reliability": dict(self.reliability),
                "companion_metrics": dict(self.companion_metrics),
                "resources": dict(self.resources),
                "verdicts": {str(k): bool(v) for k, v in self.verdicts.items()},
                "token_usage": dict(self.token_usage),
                "passed": self.passed, "notes": list(self.notes),
                "privacy": "statuses, timings, counters and boolean results only; "
                           "no prompts, responses, audio or credentials"}

    def write(self, prefix: str | Path) -> dict[str, str]:
        base = Path(prefix)
        base.parent.mkdir(parents=True, exist_ok=True)
        json_path = base.with_suffix(".json")
        md_path = base.with_suffix(".md")
        json_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=False),
                             encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return {"json": str(json_path), "markdown": str(md_path)}

    def to_markdown(self) -> str:
        data = self.to_dict()
        lines: list[str] = ["# Provider drill report", ""]
        lines.append(f"- run_id: `{self.run_id}`")
        lines.append(f"- started_at: {_fmt_time(self.started_at)}   completed_at: {_fmt_time(self.completed_at)}")
        lines.append(f"- duration_s: {data['duration_s']}")
        lines.append(f"- verdict: **{'PASS' if self.passed else 'FAIL'}**")
        lines.append("")
        lines.append("## Provider")
        lines.append("")
        lines.append("| field | value |")
        lines.append("| --- | --- |")
        for key, value in self.provider.items():
            lines.append(f"| {key} | {_cell(value)} |")
        lines.append("")
        lines.append("## Verdicts")
        lines.append("")
        lines.append("| check | result |")
        lines.append("| --- | --- |")
        for key, value in self.verdicts.items():
            lines.append(f"| {key} | {'PASS' if value else '**FAIL**'} |")
        lines.append("")
        if self.compatibility:
            lines.append("## Output-contract compatibility")
            lines.append("")
            lines.append("| purpose | status | contract_ok | samples | violations | latency_s | first_error_code |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for row in self.compatibility:
                lines.append(f"| {row.get('purpose')} | {row.get('status')} | {row.get('contract_ok')} | "
                             f"{row.get('samples')} | {row.get('violations')} | {row.get('latency_s')} | "
                             f"{row.get('first_error_code')} |")
            lines.append("")
        if self.conversation:
            conv = self.conversation
            lines.append("## Conversation continuity")
            lines.append("")
            lines.append(f"- turns: {conv.get('turn_count')}  completed: {conv.get('completed')}  "
                         f"degraded: {conv.get('degraded')}  failed: {conv.get('failed')}")
            lines.append(f"- pass_rate: {conv.get('pass_rate')}  error_rate: {conv.get('error_rate')}")
            lines.append(f"- latency p50/p95/max (s): {conv.get('latency_p50_s')} / "
                         f"{conv.get('latency_p95_s')} / {conv.get('latency_max_s')}")
            lines.append(f"- memory_recall_ok: {conv.get('memory_recall_ok')}  "
                         f"context_continuation_ok: {conv.get('context_continuation_ok')}  "
                         f"no_fabrication_ok: {conv.get('no_fabrication_ok')}")
            per_turn = conv.get("per_turn", [])
            if per_turn:
                limit = self._per_turn_limit()
                lines.append("")
                lines.append("| turn | status | passed | latency_s | chars | error_code |")
                lines.append("| --- | --- | --- | --- | --- | --- |")
                for row in per_turn[:limit]:
                    lines.append(f"| {row.get('index')} | {row.get('status')} | {row.get('passed')} | "
                                 f"{row.get('latency_s')} | {row.get('response_chars')} | {row.get('error_code')} |")
                if len(per_turn) > limit:
                    lines.append(f"| ... | ({len(per_turn) - limit} more rows in the JSON report) | | | | |")
            lines.append("")
        if self.transport_drills:
            lines.append("## Failure drills")
            lines.append("")
            for drill in self.transport_drills:
                lines.append(f"### {drill.get('name')}")
                lines.append("")
                lines.append(f"- passed: {drill.get('passed')}")
                lines.append(f"- observed: {_cell(drill.get('observed'))}")
                lines.append("")
        if self.cancellation:
            lines.append("## Cancellation and interruption")
            lines.append("")
            for key, value in self.cancellation.items():
                lines.append(f"- {key}: {_cell(value)}")
            lines.append("")
        if self.restart:
            lines.append("## Restart and recovery")
            lines.append("")
            for key, value in self.restart.items():
                lines.append(f"- {key}: {_cell(value)}")
            lines.append("")
        if self.outage_recovery:
            lines.append("## Provider outage and recovery")
            lines.append("")
            for key, value in self.outage_recovery.items():
                lines.append(f"- {key}: {_cell(value)}")
            lines.append("")
        for title, payload in (("Reliability layer totals", self.reliability),
                               ("Provider token usage (no text retained)", self.token_usage),
                               ("Companion metrics", self.companion_metrics),
                               ("Resource usage", self.resources)):
            if not payload:
                continue
            lines.append(f"## {title}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))
            lines.append("```")
            lines.append("")
        if self.notes:
            lines.append("## Notes")
            lines.append("")
            lines.extend(f"- {note}" for note in self.notes)
            lines.append("")
        lines.append("_Privacy: this report contains statuses, timings, counters and boolean results only; "
                     "no prompts, responses, audio or credentials._")
        return "\n".join(lines) + "\n"

    def _per_turn_limit(self) -> int:
        value = self.config.get("max_report_turns", 200)
        return int(value) if isinstance(value, int) else 200


def _fmt_time(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (ValueError, OSError, OverflowError):
        return str(value)


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return text if len(text) <= 400 else text[:397] + "..."
    return str(value)


def _percentile(values: Sequence[float], p: float) -> float | None:
    data = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)))
    if not data:
        return None
    return data[min(len(data) - 1, max(0, int(math.ceil(p * len(data))) - 1))]


def _latency_stats(values: Sequence[float]) -> dict[str, Any]:
    data = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not data:
        return {"count": 0}
    return {"count": len(data), "min_s": round(min(data), 4), "p50_s": round(_percentile(data, .50) or 0.0, 4),
            "p95_s": round(_percentile(data, .95) or 0.0, 4), "max_s": round(max(data), 4),
            "mean_s": round(sum(data) / len(data), 4)}


def _probe_request(*, purpose: str = "simple_response", deadline_s: float = 30.0,
                   replay_safe: bool = True, user_input: str = "drill probe") -> LLMRequest:
    now = time.monotonic()
    context = {"purpose": purpose, "user_input": user_input, "relevant_memories": [],
               "recent_dialogue": [], "interaction_rules": {}}
    return LLMRequest(request_id="drill-" + uuid.uuid4().hex, interaction_id="provider-drill",
                      intent_id=None, generation=1, purpose=purpose, created_at_mono=now,
                      deadline_mono=now + float(deadline_s),
                      context_json=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                      output_token_budget=256, replay_safe=bool(replay_safe))


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class ProviderDrillRunner:
    """Execute the acceptance drill against an injected real-provider environment."""

    def __init__(self, environment: DrillEnvironment, *, config: DrillConfig | None = None,
                 clock=time.time, monotonic=time.monotonic) -> None:
        self.environment = environment
        self.config = config or DrillConfig()
        self.clock = clock
        self.monotonic = monotonic
        self.notes: list[str] = []

    async def run(self, *, run_id: str | None = None) -> DrillReport:
        cfg = self.config
        run_id = run_id or str(uuid.uuid4())
        started = float(self.clock())
        drill_metrics = ReliabilityMetrics()
        started_tracemalloc = False
        if cfg.measure_python_memory and not tracemalloc.is_tracing():
            tracemalloc.start(5)
            started_tracemalloc = True
        sampler = ResourceSampler(loop_task_counter=self._task_count)
        await sampler.start()
        compatibility: tuple[Mapping[str, Any], ...] = ()
        conversation: dict[str, Any] = {}
        transport: list[Mapping[str, Any]] = []
        cancellation: dict[str, Any] = {}
        restart: dict[str, Any] = {}
        outage: dict[str, Any] = {}
        companion_metrics: dict[str, Any] = {}
        reliability: dict[str, Any] = {}
        token_usage: dict[str, Any] = {}
        provider_info = dict(self.environment.describe())
        try:
            session = await self.environment.create_session(label="primary")
            try:
                preflight = await self._preflight(session) if cfg.run_preflight else None
                if preflight is not None:
                    provider_info = {**provider_info, "reachable_at_start": preflight["reachable"],
                                     "preflight_latency_s": preflight["latency_s"],
                                     "preflight_status": preflight["status"]}
                    if not preflight["reachable"]:
                        self.notes.append(
                            "the provider was not reachable during the preflight check; later verdicts may "
                            "reflect provider unavailability rather than an engine defect")
                if cfg.run_compatibility:
                    compatibility = tuple(await self._run_compatibility(session))
                if cfg.run_conversation:
                    conversation = await self._run_conversation(session, run_id=run_id)
                if cfg.run_transport_drills:
                    transport = list(await self._run_transport_drills(drill_metrics))
                if cfg.run_outage_recovery:
                    outage = await self._run_outage_recovery(session)
                metrics_obj = getattr(session.companion, "metrics", None)
                snapshot_method = getattr(metrics_obj, "snapshot", None)
                if callable(snapshot_method):
                    companion_metrics = dict(snapshot_method())
                runtime_snapshot = getattr(session.runtime, "reliability_snapshot", None)
                session_snapshot = runtime_snapshot() if callable(runtime_snapshot) else None
                reliability = {"session_gateway": session_snapshot,
                               "transport_drills": drill_metrics.snapshot()}
                token_usage = self._collect_token_usage(session)
                # The restart drill creates its own sessions, so it runs while the primary
                # session is still alive.
                if cfg.run_restart:
                    restart = await self._run_restart(run_id=run_id)
                # Cancellation runs last: a hard STOP is a sticky engine latch, so running
                # it earlier would silence every later scenario on the same session.
                if cfg.run_cancellation:
                    cancellation = await self._run_cancellation(session)
            finally:
                await self.environment.close_session(session)
        finally:
            await sampler.stop()
            if started_tracemalloc:
                tracemalloc.stop()
        completed = float(self.clock())
        verdicts = self._verdicts(compatibility, conversation, transport, cancellation, restart, outage)
        return DrillReport(
            schema_version="1.0", run_id=run_id, started_at=started, completed_at=completed,
            provider=provider_info,
            config={"turns": cfg.turns, "max_report_turns": cfg.max_report_turns,
                    "retry_policy": cfg.retry_policy.to_dict(),
                    "circuit_failure_threshold": cfg.circuit_failure_threshold,
                    "circuit_recovery_timeout_s": cfg.circuit_recovery_timeout_s,
                    "deadline_timeout_s": cfg.deadline_timeout_s,
                    "scenarios": {"compatibility": cfg.run_compatibility, "conversation": cfg.run_conversation,
                                  "transport_drills": cfg.run_transport_drills,
                                  "cancellation": cfg.run_cancellation,
                                  "outage_recovery": cfg.run_outage_recovery, "restart": cfg.run_restart}},
            compatibility=compatibility, conversation=conversation,
            transport_drills=tuple(transport), cancellation=cancellation, restart=restart,
            outage_recovery=outage, reliability=reliability, companion_metrics=companion_metrics,
            resources=sampler.summary(), verdicts=verdicts, token_usage=token_usage,
            notes=tuple(self.notes))

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _collect_token_usage(session: DrillSession) -> dict[str, Any]:
        """Read provider-reported token totals from the session's raw adapter."""
        delegate = getattr(session.fault_adapter, "delegate", None)
        snapshot = getattr(delegate, "usage_snapshot", None)
        if not callable(snapshot):
            return {}
        try:
            usage = snapshot()
        except Exception:
            return {}
        return dict(usage) if isinstance(usage, Mapping) else {}

    @staticmethod
    def _task_count() -> int:
        try:
            return len(asyncio.all_tasks())
        except RuntimeError:
            return 0

    def _verdicts(self, compat, conversation, transport, cancellation, restart, outage) -> dict[str, bool]:
        verdicts: dict[str, bool] = {}
        if compat:
            contract_rows = [row for row in compat if row.get("purpose") != "provider_metadata"]
            if contract_rows:
                verdicts["output_contract_all_purposes"] = all(bool(row.get("contract_ok"))
                                                              for row in contract_rows)
            metadata_rows = [row for row in compat if row.get("purpose") == "provider_metadata"]
            if metadata_rows and self.config.expect_thinking_disabled:
                verdicts["thinking_mode_disabled"] = bool(metadata_rows[0].get("contract_ok"))
        if conversation:
            verdicts["long_conversation_no_hard_failures"] = int(conversation.get("failed", 1)) == 0
            verdicts["memory_recall_across_turns"] = bool(conversation.get("memory_recall_ok"))
            verdicts["context_continuation"] = bool(conversation.get("context_continuation_ok"))
            verdicts["no_fabrication_without_live_data"] = bool(conversation.get("no_fabrication_ok"))
        for drill in transport:
            verdicts[str(drill.get("name"))] = bool(drill.get("passed"))
        if cancellation:
            verdicts["cancellation_no_leaked_tasks"] = bool(cancellation.get("no_leaked_tasks"))
            # Reported separately so a cancelled-too-late race is visible, not silently green.
            verdicts["cancellation_was_inflight"] = bool(
                cancellation.get("request_inflight_when_cancelled")
                and cancellation.get("control_request_inflight_when_stopped"))
            verdicts["control_lane_interrupts_inflight"] = bool(cancellation.get("control_interrupted"))
        if restart:
            verdicts["restart_restores_context"] = bool(restart.get("context_restored"))
        if outage:
            verdicts["outage_failures_were_honest"] = bool(outage.get("failures_were_honest"))
            verdicts["service_recovers_after_outage"] = bool(outage.get("recovered"))
        return verdicts

    # -- preflight -------------------------------------------------------------
    async def _preflight(self, session: DrillSession) -> dict[str, Any]:
        """End-to-end readiness check through the engine's own dialogue path."""
        cfg = self.config
        start = float(self.monotonic())
        status, error = "EXCEPTION", None
        try:
            outcome = await asyncio.wait_for(
                session.companion.chat("连通性自检，请回复“就绪”。", interaction_id="drill-preflight"),
                timeout=cfg.probe_timeout_s)
            status = str(getattr(getattr(outcome, "status", None), "value",
                                 getattr(outcome, "status", None)))
        except asyncio.TimeoutError:
            status, error = "TIMED_OUT", "PREFLIGHT_TIMEOUT"
        except Exception as exc:
            error = type(exc).__name__
        latency = max(0.0, float(self.monotonic()) - start)
        return {"reachable": status == "COMPLETED", "status": status,
                "latency_s": round(latency, 4), "error_code": error}

    # -- compatibility ---------------------------------------------------------
    async def _run_compatibility(self, session: DrillSession) -> list[Mapping[str, Any]]:
        cfg = self.config
        engine = session.engine
        rows: list[Mapping[str, Any]] = []
        probes: list[tuple[str, Any]] = [
            ("demand_understanding", lambda: engine.understand_demand(
                user_input="帮我记一下明天下午三点要开会。", interaction_id="drill-compat-demand", generation=1)),
            ("simple_response", lambda: engine.request_llm(
                purpose="simple_response", interaction_id="drill-compat-simple", generation=1,
                user_input="你好，简单介绍一下你自己。")),
            ("complex_dialogue", lambda: engine.request_llm(
                purpose="complex_dialogue", interaction_id="drill-compat-complex", generation=1,
                user_input="我最近有点累，但又不想放弃跑步，你觉得我该怎么安排这周？",
                recent_dialogue=[{"role": "user", "text": "我想开始跑步。"},
                                 {"role": "assistant", "text": "可以从每周两次开始。"}])),
            ("task_plan", lambda: engine.request_llm(
                purpose="task_plan", interaction_id="drill-compat-task", generation=1,
                goal="用语音提醒用户十分钟后起来活动一下。", max_steps=3,
                allowed_action_types=["SPEAK", "WAIT", "NOTIFY", "LIGHT"])),
            ("multi_step_plan", lambda: engine.request_llm(
                purpose="multi_step_plan", interaction_id="drill-compat-multi", generation=1,
                goal="先核对房间的关键环境信息，再决定是否点亮指示灯。", allowed_tools=[],
                constraints=["no_tool_execution_available"])),
            ("memory_consolidation", lambda: engine.request_llm(
                purpose="memory_consolidation", interaction_id="drill-compat-memory", generation=1,
                time_range={"start_wall": 0.0, "end_wall": 1.0},
                candidate_events=[{"event_id": "evt-drill-1", "kind": "user_message",
                                   "timestamp": 0.0, "summary": "用户提到这周要准备马拉松。",
                                   "source_refs": ["evt-drill-1"], "fact_status": "candidate"}])),
        ]
        for purpose, factory in probes:
            samples: list[tuple[str, bool, str | None]] = []
            latencies: list[float] = []
            for _ in range(cfg.compat_attempts):
                start = float(self.monotonic())
                status, contract_ok, error_code = "FAILED", False, None
                try:
                    result = await asyncio.wait_for(factory(), timeout=cfg.compat_timeout_s)
                except asyncio.TimeoutError:
                    status, error_code = "TIMED_OUT", "DRILL_COMPAT_TIMEOUT"
                    self.notes.append(f"compatibility probe {purpose} exceeded {cfg.compat_timeout_s}s")
                except Exception as exc:
                    status, error_code = "FAILED", type(exc).__name__
                else:
                    status, contract_ok, error_code = self._classify_compat(purpose, result)
                latencies.append(max(0.0, float(self.monotonic()) - start))
                samples.append((status, contract_ok, error_code))
                if contract_ok:
                    break  # stop resampling once the provider demonstrably complies
            violations = sum(1 for _s, ok, _e in samples if not ok)
            first_error = next((code for _s, ok, code in samples if not ok and code), None)
            rows.append({
                "purpose": purpose,
                "status": samples[-1][0],
                "contract_ok": any(ok for _s, ok, _e in samples),
                "samples": len(samples), "violations": violations,
                "violation_rate": round(violations / len(samples), 4),
                "first_error_code": first_error,
                "latency_s": round(_percentile(latencies, .50) or 0.0, 4),
            })
        metadata = await self._check_provider_metadata()
        if metadata is not None:
            rows.append(metadata)
        return rows

    async def _check_provider_metadata(self) -> Mapping[str, Any] | None:
        """Verify provider-reported metadata: model id, token usage and thinking mode."""
        cfg = self.config
        adapter = await self.environment.create_fault_adapter(label="metadata")
        start = float(self.monotonic())
        ok, error = False, None
        try:
            payload = await adapter.generate(_probe_request(deadline_s=cfg.probe_timeout_s))
            ok = isinstance(payload, Mapping) and isinstance(payload.get("response_text"), str)
        except Exception as exc:
            error = type(exc).__name__
        latency = max(0.0, float(self.monotonic()) - start)
        usage = getattr(adapter.delegate, "last_usage", None)
        await self.environment.close_fault_adapter(adapter)
        if not isinstance(usage, Mapping):
            usage = {}
        reasoning = usage.get("reasoning_tokens")
        thinking_ok: bool | None = None
        if cfg.expect_thinking_disabled:
            thinking_ok = bool(ok and isinstance(reasoning, int) and reasoning == 0)
            if reasoning is None:
                self.notes.append("provider did not report completion_tokens_details.reasoning_tokens; "
                                  "thinking mode could not be verified by token accounting")
        else:
            thinking_ok = ok
        return {"purpose": "provider_metadata", "status": "COMPLETED" if ok else "FAILED",
                "contract_ok": bool(thinking_ok), "samples": 1, "violations": 0 if ok else 1,
                "violation_rate": 0.0 if ok else 1.0, "first_error_code": error,
                "latency_s": round(latency, 4), "reported_model": usage.get("model"),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": reasoning, "thinking_disabled_verified": thinking_ok}

    @staticmethod
    def _classify_compat(purpose: str, result: Any) -> tuple[str, bool, str | None]:
        """Normalize a gateway/interpreter result into (status, contract_ok, error_code)."""
        if purpose == "demand_understanding":
            if not isinstance(result, Mapping):
                return "FAILED", False, "DRILL_DEMAND_NOT_MAPPING"
            expected = {"schema_version", "request_type", "intent", "entities", "emotion_assessment",
                        "clarification_needed", "response_candidate"}
            if set(result) == expected and result.get("schema_version") == "1.0":
                return "COMPLETED", True, None
            return "REJECTED", False, "DRILL_DEMAND_SCHEMA_MISMATCH"
        status = getattr(result, "status", None)
        code = getattr(result, "error_code", None)
        value = getattr(status, "value", status)
        if value == LLMStatus.COMPLETED.value:
            if code == "DEGRADED_LOCAL_FALLBACK":
                return "DEGRADED", False, "DEGRADED_LOCAL_FALLBACK"
            return "COMPLETED", True, None
        return str(value or "FAILED"), False, (str(code) if code else None)

    # -- conversation ----------------------------------------------------------
    def _conversation_script(self) -> list[tuple[str, dict[str, Any]]]:
        """Build the authored turn script with two long-range memory/context probes."""
        cfg = self.config
        specials: dict[int, tuple[str, dict[str, Any]]] = {
            cfg.memory_expected_turn_index: (cfg.memory_question,
                                             {"required_any": cfg.memory_expected_tokens, "min_chars": 2,
                                              "label": "memory_recall"}),
            cfg.context_expected_turn_index: (cfg.context_question,
                                              {"required_any": cfg.context_expected_tokens, "min_chars": 2,
                                               "label": "context_continuation"}),
        }
        lead_index = max(1, cfg.context_expected_turn_index - 3)
        if lead_index not in specials and lead_index != 0 and lead_index < cfg.turns:
            specials[lead_index] = (cfg.context_statement, {})
        script: list[tuple[str, dict[str, Any]]] = []
        for index in range(cfg.turns):
            if index == 0:
                script.append((cfg.memory_statement, {}))
            elif index in specials:
                script.append(specials[index])
            else:
                pool = _CONVERSATION_BASE if index % 2 == 0 else _CONVERSATION_FILLER
                text, checks = pool[(index // 2) % len(pool)]
                script.append((text, dict(checks)))
        return script

    async def _run_conversation(self, session: DrillSession, *, run_id: str) -> dict[str, Any]:
        cfg = self.config
        companion = session.companion
        interaction_id = f"drill-conversation-{run_id[:8]}"
        script = self._conversation_script()
        per_turn: list[dict[str, Any]] = []
        latencies: list[float] = []
        memory_recall_ok = False
        context_ok = False
        fabrication_ok = True
        started = float(self.monotonic())
        for index, (text, checks) in enumerate(script):
            start = float(self.monotonic())
            labelled = bool(checks.get("label"))
            # A labelled probe (memory recall / context continuation) asserts a capability,
            # not provider availability, so it gets a bounded retry budget. Filler turns
            # stay single-shot so the conversation stays realistic.
            attempts_allowed = cfg.probe_turn_attempts if labelled else 1
            record: dict[str, Any] = {"index": index, "status": "EXCEPTION", "passed": False,
                                      "latency_s": 0.0, "response_chars": 0, "error_code": None,
                                      "degraded": False, "label": checks.get("label"), "attempts": 0}
            try:
                for attempt in range(1, attempts_allowed + 1):
                    record["attempts"] = attempt
                    start = float(self.monotonic())
                    outcome = await companion.chat(text, interaction_id=interaction_id)
                    elapsed = max(0.0, float(self.monotonic()) - start)
                    latencies.append(elapsed)
                    status = str(getattr(getattr(outcome, "status", "FAILED"), "value",
                                         getattr(outcome, "status", "FAILED")))
                    response = str(getattr(outcome, "response_text", "") or "")
                    error_code = getattr(outcome, "error_code", None)
                    degraded = bool(getattr(outcome, "degraded", False)) or error_code == "DEGRADED_LOCAL_FALLBACK"
                    ok_status = status in _CONVERSATION_OK_STATUSES
                    required_ok = not checks.get("required_any") or any(t in response for t in checks["required_any"])
                    forbidden_ok = not any(t in response for t in checks.get("forbidden_any", ()))
                    length_ok = len(response.strip()) >= int(checks.get("min_chars", 1))
                    passed = bool(ok_status and not degraded and required_ok
                                  and forbidden_ok and length_ok and response.strip())
                    record.update({"status": status, "passed": passed, "latency_s": round(elapsed, 4),
                                   "response_chars": len(response), "error_code": error_code,
                                   "degraded": degraded, "required_ok": required_ok,
                                   "forbidden_ok": forbidden_ok, "length_ok": length_ok})
                    if checks.get("label") == "memory_recall" and required_ok:
                        memory_recall_ok = True
                    if checks.get("label") == "context_continuation" and required_ok:
                        context_ok = True
                    if checks.get("forbidden_any") and not forbidden_ok:
                        fabrication_ok = False
                    if passed or attempt >= attempts_allowed:
                        break
                    await asyncio.sleep(cfg.cancellation_settle_s)
                if not record["passed"] and labelled:
                    self.notes.append(f"conversation probe '{checks.get('label')}' did not verify within "
                                      f"{attempts_allowed} attempts (status {record['status']})")
            except Exception as exc:
                elapsed = max(0.0, float(self.monotonic()) - start)
                latencies.append(elapsed)
                record.update({"status": "EXCEPTION", "passed": False, "error_code": type(exc).__name__,
                               "latency_s": round(elapsed, 4)})
            per_turn.append(record)
        completed = sum(1 for row in per_turn if row["status"] == "COMPLETED")
        degraded_count = sum(1 for row in per_turn if row.get("degraded"))
        # Hard failure = no answer at all. A disclosed fallback is honest degradation and
        # is reported separately, so a real provider incident does not masquerade as a
        # conversation-continuity defect.
        failed = sum(1 for row in per_turn
                     if row["status"] not in _CONVERSATION_OK_STATUSES
                     and row["status"] not in _CONVERSATION_SOFT_STATUSES)
        passed_turns = sum(1 for row in per_turn if row["passed"])
        return {
            "interaction_id": interaction_id,
            "turn_count": len(per_turn), "completed": completed, "degraded": degraded_count,
            "failed": failed, "passed_turns": passed_turns,
            "pass_rate": round(passed_turns / len(per_turn), 4) if per_turn else 0.0,
            "degradation_rate": round(degraded_count / len(per_turn), 4) if per_turn else 0.0,
            "error_rate": round(failed / len(per_turn), 4) if per_turn else 0.0,
            "latency_p50_s": round(_percentile(latencies, .50) or 0.0, 4),
            "latency_p95_s": round(_percentile(latencies, .95) or 0.0, 4),
            "latency_max_s": round(max(latencies), 4) if latencies else 0.0,
            "latency_stats": _latency_stats(latencies),
            "wall_duration_s": round(max(0.0, float(self.monotonic()) - started), 4),
            "memory_recall_ok": memory_recall_ok, "context_continuation_ok": context_ok,
            "no_fabrication_ok": fabrication_ok, "per_turn": per_turn,
        }

    # -- transport drills ------------------------------------------------------
    async def _run_transport_drills(self, metrics: ReliabilityMetrics) -> list[Mapping[str, Any]]:
        return [
            await self._drill_controlled_retry(metrics),
            await self._drill_non_retryable(metrics),
            await self._drill_no_replay_suppression(metrics),
            await self._drill_circuit_breaker(metrics),
            await self._drill_deadline_timeout(metrics),
        ]

    async def _drill_controlled_retry(self, metrics: ReliabilityMetrics) -> Mapping[str, Any]:
        cfg = self.config
        faults = [FaultSpec(FAULT_RATE_LIMIT, retry_after_s=1.0, label="rate_limit"),
                  FaultSpec(FAULT_SERVER, status_code=503, label="server_error"),
                  FaultSpec(FAULT_CONNECT, label="connect_reset")]
        adapter = await self.environment.create_fault_adapter(label="controlled-retry", faults=faults)
        local = ReliabilityMetrics()
        # Three injected faults need four attempts; widen the budget only for this drill so
        # the configured production policy is not silently changed.
        policy = RetryPolicy(max_attempts=max(4, cfg.retry_policy.max_attempts),
                             base_delay_s=cfg.retry_policy.base_delay_s,
                             max_delay_s=cfg.retry_policy.max_delay_s,
                             max_total_retry_wait_s=cfg.retry_policy.max_total_retry_wait_s,
                             jitter_ratio=cfg.retry_policy.jitter_ratio,
                             respect_retry_after=cfg.retry_policy.respect_retry_after,
                             retry_kinds=cfg.retry_policy.retry_kinds)
        reliable = ReliableLLMAdapter(adapter, policy=policy, metrics=local)
        start = float(self.monotonic())
        passed, error = False, None
        try:
            payload = await reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s))
            passed = isinstance(payload, Mapping) and isinstance(payload.get("response_text"), str)
        except Exception as exc:
            error = type(exc).__name__
        elapsed = max(0.0, float(self.monotonic()) - start)
        snapshot = local.snapshot()
        metrics.absorb(local)
        await self.environment.close_fault_adapter(adapter)
        return {
            "name": "controlled_retry_recovers_from_transient_faults",
            "passed": bool(passed and snapshot["retries"] >= 3 and snapshot["failures"] >= 3),
            "observed": {"injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"],
                         "succeeded": passed, "error": error, "wall_s": round(elapsed, 4),
                         "attempts": snapshot["attempts"], "retries": snapshot["retries"],
                         "retries_by_kind": snapshot["retries_by_kind"],
                         "retry_wait_s": snapshot["retry_wait_s"],
                         "retry_after_observed_s": snapshot["retry_after_observed_s"],
                         "rate_limit_waits_s": snapshot["rate_limit_waits_s"]},
        }

    async def _drill_non_retryable(self, metrics: ReliabilityMetrics) -> Mapping[str, Any]:
        cfg = self.config
        adapter = await self.environment.create_fault_adapter(
            label="non-retryable", faults=[FaultSpec(FAULT_AUTH, status_code=401)])
        local = ReliabilityMetrics()
        reliable = ReliableLLMAdapter(adapter, policy=cfg.retry_policy, metrics=local)
        start = float(self.monotonic())
        error, passed = None, False
        try:
            await reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s))
        except Exception as exc:  # expected
            error = type(exc).__name__
            passed = local.retries == 0 and local.attempts == 1
        elapsed = max(0.0, float(self.monotonic()) - start)
        snapshot = local.snapshot()
        metrics.absorb(local)
        await self.environment.close_fault_adapter(adapter)
        return {
            "name": "authentication_failure_is_not_retried",
            "passed": passed,
            "observed": {"injected_fault": "AUTH(401)", "raised": error,
                         "attempts": snapshot["attempts"], "retries": snapshot["retries"],
                         "failures_by_kind": snapshot["failures_by_kind"],
                         "no_fabricated_success": error is not None,
                         "wall_s": round(elapsed, 4)},
        }

    async def _drill_no_replay_suppression(self, metrics: ReliabilityMetrics) -> Mapping[str, Any]:
        cfg = self.config
        adapter = await self.environment.create_fault_adapter(
            label="no-replay", faults=[FaultSpec(FAULT_RATE_LIMIT, retry_after_s=0.2)])
        local = ReliabilityMetrics()
        reliable = ReliableLLMAdapter(adapter, policy=cfg.retry_policy, metrics=local)
        error, suppressed = None, False
        try:
            await reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s, replay_safe=False))
        except Exception as exc:
            error = type(exc).__name__
            suppressed = local.retries == 0 and local.retries_suppressed_no_replay == 1 and local.attempts == 1
        snapshot = local.snapshot()
        metrics.absorb(local)
        await self.environment.close_fault_adapter(adapter)
        return {
            "name": "side_effecting_request_is_never_replayed",
            "passed": suppressed,
            "observed": {"injected_fault": "RATE_LIMIT(retry_after=0.2s)", "request_replay_safe": False,
                         "raised": error, "attempts": snapshot["attempts"], "retries": snapshot["retries"],
                         "retries_suppressed_no_replay": snapshot["retries_suppressed_no_replay"]},
        }

    async def _drill_circuit_breaker(self, metrics: ReliabilityMetrics) -> Mapping[str, Any]:
        cfg = self.config
        threshold = cfg.circuit_failure_threshold
        # Slow network-style failures make the fast-fail comparison meaningful.
        adapter = await self.environment.create_fault_adapter(
            label="circuit",
            faults=[FaultSpec(FAULT_CONNECT, count=threshold + 2, delay_s=0.15)])
        policy = RetryPolicy(max_attempts=1, base_delay_s=0.1, max_delay_s=0.5, jitter_ratio=0.0)
        local = ReliabilityMetrics()
        breaker = CircuitBreaker(failure_threshold=threshold, recovery_timeout_s=cfg.circuit_recovery_timeout_s)
        reliable = ReliableLLMAdapter(adapter, policy=policy, breaker=breaker, metrics=local)
        fail_latencies: list[float] = []
        for _ in range(threshold + 1):
            start = float(self.monotonic())
            try:
                await reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s))
            except Exception:
                pass
            fail_latencies.append(max(0.0, float(self.monotonic()) - start))
        opened_state = breaker.state.value
        fast_fail_latency = fail_latencies[-1]
        network_fail_latency = max(fail_latencies[:-1]) if len(fail_latencies) > 1 else fail_latencies[0]
        adapter.clear_faults()
        recovery_start = float(self.monotonic())
        recovered = False
        while (float(self.monotonic()) - recovery_start) < cfg.circuit_recovery_timeout_s + 3.0:
            if breaker.state.value != "OPEN":
                break
            await asyncio.sleep(0.1)
        waited_s = max(0.0, float(self.monotonic()) - recovery_start)
        probe_start = float(self.monotonic())
        try:
            payload = await reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s))
            recovered = isinstance(payload, Mapping) and isinstance(payload.get("response_text"), str)
        except Exception:
            recovered = False
        probe_latency = max(0.0, float(self.monotonic()) - probe_start)
        closed_state = breaker.state.value
        snapshot = local.snapshot()
        metrics.absorb(local)
        await self.environment.close_fault_adapter(adapter)
        passed = bool(opened_state == "OPEN" and snapshot["circuit_rejections"] >= 1
                      and recovered and closed_state == "CLOSED"
                      and fast_fail_latency <= max(0.02, network_fail_latency * 0.5))
        return {
            "name": "circuit_opens_during_outage_and_recovers",
            "passed": passed,
            "observed": {"outage_requests": threshold + 1, "state_after_outage": opened_state,
                         "fail_fast_latency_s": round(fast_fail_latency, 4),
                         "network_fail_latency_s": round(network_fail_latency, 4),
                         "circuit_rejections": snapshot["circuit_rejections"],
                         "waited_for_recovery_s": round(waited_s, 4),
                         "half_open_probe_succeeded": recovered, "probe_latency_s": round(probe_latency, 4),
                         "state_after_recovery": closed_state},
        }

    async def _drill_deadline_timeout(self, metrics: ReliabilityMetrics) -> Mapping[str, Any]:
        cfg = self.config
        # A deterministic injected delay makes the deadline drill reproducible; the
        # follow-up call still exercises the live provider end to end.
        adapter = await self.environment.create_fault_adapter(
            label="deadline",
            faults=[FaultSpec(FAULT_TIMEOUT, delay_s=cfg.deadline_timeout_s + 1.5)])
        local = ReliabilityMetrics()
        policy = RetryPolicy(max_attempts=1, base_delay_s=0.1, max_delay_s=0.5, jitter_ratio=0.0)
        reliable = ReliableLLMAdapter(adapter, policy=policy, metrics=local)
        start = float(self.monotonic())
        timed_out = False
        try:
            await asyncio.wait_for(reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s)),
                                   timeout=cfg.deadline_timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
        except Exception:
            timed_out = False
        elapsed = max(0.0, float(self.monotonic()) - start)
        # A normal call afterwards must still succeed: a timeout must not poison the client.
        healthy = False
        start2 = float(self.monotonic())
        try:
            payload = await reliable.generate(_probe_request(deadline_s=cfg.probe_timeout_s))
            healthy = isinstance(payload, Mapping) and isinstance(payload.get("response_text"), str)
        except Exception:
            healthy = False
        followup_s = max(0.0, float(self.monotonic()) - start2)
        snapshot = local.snapshot()
        metrics.absorb(local)
        await self.environment.close_fault_adapter(adapter)
        bounding_ok = elapsed <= cfg.deadline_timeout_s + 1.0
        if not bounding_ok:
            self.notes.append(f"deadline drill exceeded the expected bound: {elapsed:.3f}s")
        return {
            "name": "client_deadline_is_bounded_and_client_still_usable",
            "passed": bool(timed_out and bounding_ok and healthy),
            "observed": {"deadline_s": cfg.deadline_timeout_s, "observed_timeout_s": round(elapsed, 4),
                         "raised_timeout": timed_out, "followup_call_succeeded": healthy,
                         "followup_latency_s": round(followup_s, 4),
                         "attempts": snapshot["attempts"]},
        }

    # -- cancellation ----------------------------------------------------------
    def _reset_session_circuit(self, session: DrillSession) -> None:
        """Clear breaker state so a cancellation drill starts from a usable provider."""
        breaker = getattr(getattr(session.runtime, "reliability", None), "breaker", None)
        if breaker is not None:
            breaker.reset()

    async def _run_cancellation(self, session: DrillSession) -> dict[str, Any]:
        cfg = self.config
        companion = session.companion
        fault_adapter = session.fault_adapter
        self._reset_session_circuit(session)
        # A slow injected fault keeps each request in flight long enough that cancellation
        # is deterministic even when the live provider is answering in milliseconds.
        if fault_adapter is not None:
            fault_adapter.set_faults([FaultSpec(FAULT_TIMEOUT, count=16, delay_s=cfg.cancellation_inflight_s)])
        before = self._task_count()
        try:
            interaction_id = f"drill-cancel-{uuid.uuid4().hex[:8]}"
            task = asyncio.create_task(companion.chat(
                "请用稍微长一点的方式介绍一下你自己，然后再讲讲专注的价值。", interaction_id=interaction_id))
            await asyncio.sleep(min(0.35, max(0.05, cfg.cancellation_settle_s / 3)))
            was_inflight = not task.done()
            task.cancel()
            raised = None
            try:
                await task
            except asyncio.CancelledError:
                raised = "CancelledError"
            except Exception as exc:
                raised = type(exc).__name__
            await asyncio.sleep(cfg.cancellation_settle_s)
            after = self._task_count()

            # Control-lane interruption: a hard STOP must invalidate an in-flight generation.
            control_interaction = f"drill-control-{uuid.uuid4().hex[:8]}"
            control_task = asyncio.create_task(companion.chat("给我讲一个比较长的小故事。",
                                                              interaction_id=control_interaction))
            await asyncio.sleep(min(0.35, max(0.05, cfg.cancellation_settle_s / 3)))
            control_was_inflight = not control_task.done()
            control_result = await companion.chat("停止", interaction_id=control_interaction)
            control_status = str(getattr(getattr(control_result, "status", None), "value",
                                         getattr(control_result, "status", None)))
            control_interrupted = False
            control_outcome = None
            try:
                outcome = await asyncio.wait_for(control_task, timeout=cfg.probe_timeout_s)
                control_outcome = str(getattr(getattr(outcome, "status", None), "value",
                                             getattr(outcome, "status", None)))
                # The STOP must prevent a live answer from surfacing after the interruption.
                control_interrupted = control_outcome not in {"COMPLETED"}
            except asyncio.TimeoutError:
                control_task.cancel()
                self.notes.append("control-lane interruption did not settle within the probe timeout")
            except asyncio.CancelledError:
                control_outcome = "CANCELLED"
                control_interrupted = True
            await asyncio.sleep(cfg.cancellation_settle_s)
            final_tasks = self._task_count()
        finally:
            if fault_adapter is not None:
                fault_adapter.clear_faults()
        return {
            "mode": "asyncio task cancellation mid-flight plus control-lane STOP "
                    "(injected slow fault keeps both requests in flight)",
            "request_inflight_when_cancelled": was_inflight,
            "control_request_inflight_when_stopped": control_was_inflight,
            "tasks_before": before, "tasks_after_cancel": after, "tasks_final": final_tasks,
            "no_leaked_tasks": final_tasks <= before + 1,
            "cancel_raised": raised, "control_stop_status": control_status,
            "control_outcome_status": control_outcome,
            "control_interrupted": control_interrupted,
        }

    # -- outage on the companion path -----------------------------------------
    async def _run_outage_recovery(self, session: DrillSession) -> dict[str, Any]:
        cfg = self.config
        fault_adapter = session.fault_adapter
        if fault_adapter is None:
            return {"skipped": True, "reason": "session does not expose a fault adapter"}
        companion = session.companion
        interaction_id = f"drill-outage-{uuid.uuid4().hex[:8]}"
        fault_adapter.set_outage(True)
        statuses: list[str] = []
        degraded: list[bool] = []
        honest = True
        try:
            for _ in range(2):
                outcome = await companion.chat("在服务中断期间随便说点什么。", interaction_id=interaction_id)
                status = str(getattr(getattr(outcome, "status", None), "value",
                                     getattr(outcome, "status", None)))
                statuses.append(status)
                is_degraded = bool(getattr(outcome, "degraded", False)) or (
                    getattr(outcome, "error_code", None) == "DEGRADED_LOCAL_FALLBACK")
                degraded.append(is_degraded)
                # Honest behavior: an explicit failure/stale/silenced result, or a clearly
                # disclosed local fallback. Never a silent fabricated "live" answer.
                if status not in {"FAILED", "STALE", "SILENCED"} and not is_degraded:
                    honest = False
        finally:
            fault_adapter.set_outage(False)
        # The session circuit breaker may still be open; wait for its recovery window.
        breaker = getattr(getattr(session.runtime, "reliability", None), "breaker", None)
        waited = 0.0
        if breaker is not None:
            start_wait = float(self.monotonic())
            while (float(self.monotonic()) - start_wait) < cfg.circuit_recovery_timeout_s + 3.0:
                if breaker.state.value != "OPEN":
                    break
                await asyncio.sleep(0.1)
            waited = max(0.0, float(self.monotonic()) - start_wait)
        # Bounded retries: immediately after a real outage the provider itself may still be
        # unhealthy, which is not an engine-recovery defect. The engine's own controlled
        # retry is given a fair window before the verdict is decided.
        recovery_status, recovery_s, recovered = None, 0.0, False
        attempts = 0
        for _ in range(cfg.outage_recovery_attempts):
            attempts += 1
            start = float(self.monotonic())
            outcome = await companion.chat("服务恢复了，请简单打个招呼。", interaction_id=interaction_id)
            recovery_s = max(0.0, float(self.monotonic()) - start)
            recovery_status = str(getattr(getattr(outcome, "status", None), "value",
                                          getattr(outcome, "status", None)))
            if recovery_status == "COMPLETED" and bool(str(getattr(outcome, "response_text", "")).strip()):
                recovered = True
                break
            await asyncio.sleep(cfg.cancellation_settle_s)
        if not recovered:
            self.notes.append("the provider was still not answering after the injected outage ended; "
                              "the engine degraded honestly, so 'recovery' could not be observed in this run")
        return {"outage_turns": len(statuses), "outage_statuses": statuses,
                "outage_used_disclosed_degradation": degraded, "failures_were_honest": honest,
                "waited_for_circuit_recovery_s": round(waited, 4),
                "recovery_attempts": attempts, "recovered": recovered,
                "recovery_status": recovery_status, "recovery_latency_s": round(recovery_s, 4)}

    # -- restart ---------------------------------------------------------------
    async def _run_restart(self, *, run_id: str) -> dict[str, Any]:
        cfg = self.config
        interaction_id = f"drill-restart-{run_id[:8]}"
        session = await self.environment.create_session(label="restart")
        try:
            await session.companion.chat(cfg.memory_statement, interaction_id=interaction_id)
            await session.companion.chat("我们等会儿再聊。", interaction_id=interaction_id)
            turns_before_close = len(session.companion.history(interaction_id))
        finally:
            await self.environment.close_session(session)
        # A fresh process-equivalent session must reuse the persisted memory and transcript.
        session2 = await self.environment.create_session(label="restart-2")
        try:
            recall_ok, status, attempts = False, None, 0
            for _ in range(cfg.restart_attempts):
                attempts += 1
                outcome = await session2.companion.chat(cfg.memory_question, interaction_id=interaction_id)
                response = str(getattr(outcome, "response_text", "") or "")
                status = str(getattr(getattr(outcome, "status", None), "value",
                                     getattr(outcome, "status", None)))
                if status == "COMPLETED" and any(token in response for token in cfg.restart_check_tokens):
                    recall_ok = True
                    break
                await asyncio.sleep(cfg.cancellation_settle_s)
            if not recall_ok:
                self.notes.append("post-restart recall did not produce a verified answer within the retry "
                                  "budget (provider availability is not distinguished from memory loss here)")
            turns_after_restart = len(session2.companion.history(interaction_id))
            return {"recall_after_restart_ok": recall_ok, "status": status, "attempts": attempts,
                    "turns_before_close": turns_before_close,
                    "turns_after_restart": turns_after_restart,
                    "context_restored": bool(recall_ok and turns_after_restart >= 3)}
        finally:
            await self.environment.close_session(session2)


__all__ = [
    "DrillConfig", "DrillReport", "DrillSession", "DrillEnvironment", "ProviderDrillRunner",
    "FaultSpec", "FaultInjectingAdapter", "ResourceSampler", "expand_faults",
    "FAULT_RATE_LIMIT", "FAULT_SERVER", "FAULT_TIMEOUT", "FAULT_CONNECT", "FAULT_AUTH",
]
