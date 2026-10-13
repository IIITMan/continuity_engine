"""Deterministic tests for provider reliability, adapter transport and drill harness.

Every test here uses fake adapters or httpx.MockTransport. No real endpoint is
contacted and no credential is read from the environment.
"""
from __future__ import annotations

import asyncio
import json
import math
import time

import httpx
import pytest

from continuity_engine import (
    CircuitBreaker, CircuitOpenError, ContinuityEngine, FailureKind, LLMGateway, LLMRequest, LLMStatus,
    OpenAICompatibleAdapter, ProviderAuthError, ProviderConfigurationError, ProviderDrillRunner,
    ProviderRateLimitError, ProviderRequestError, ProviderServerError, ProviderTimeoutError,
    ReliabilityMetrics, ReliableLLMAdapter, RetryPolicy, classify_provider_failure, backoff_delay,
    parse_retry_after_header, retry_after_seconds,
)
from continuity_engine.validation.provider_drill import (
    DrillConfig, DrillReport, FAULT_AUTH, FAULT_RATE_LIMIT, FAULT_SERVER, FAULT_TIMEOUT,
    FaultInjectingAdapter, FaultSpec, ResourceSampler, expand_faults,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def probe_request(*, purpose="simple_response", deadline_s=30.0, replay_safe=True) -> LLMRequest:
    now = time.monotonic()
    context = {"purpose": purpose, "user_input": "hello", "relevant_memories": [],
               "recent_dialogue": [], "interaction_rules": {}}
    return LLMRequest(request_id="req", interaction_id="iid", intent_id=None, generation=1,
                      purpose=purpose, created_at_mono=now, deadline_mono=now + deadline_s,
                      context_json=json.dumps(context), output_token_budget=64, replay_safe=replay_safe)


class StubAdapter:
    """Deterministic adapter driven by a queue of payloads or exceptions."""

    is_replay_adapter = False
    model = "stub-model"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    async def generate(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    async def aclose(self):
        self.closed = True


def mock_adapter(handler, **kwargs) -> tuple[OpenAICompatibleAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(endpoint_url="https://provider.invalid/v1/chat/completions",
                                      model="test-model", api_key="test-secret",
                                      timeout_s=5, client=client, **kwargs)
    return adapter, client


# --------------------------------------------------------------------------- #
# Policy and classification
# --------------------------------------------------------------------------- #
def test_retry_policy_validates_bounds():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=9)
    with pytest.raises(ValueError):
        RetryPolicy(jitter_ratio=1.5)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_s=5.0, max_delay_s=1.0)
    with pytest.raises(ValueError):
        RetryPolicy(retry_kinds=frozenset({"NOT_A_KIND"}))
    policy = RetryPolicy()
    assert policy.retries(FailureKind.RATE_LIMIT)
    assert not policy.retries(FailureKind.AUTH)
    assert policy.to_dict()["max_attempts"] == 3


def test_backoff_delay_grows_and_respects_retry_after():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=10.0, jitter_ratio=0.0)
    assert backoff_delay(1, policy) == 1.0
    assert backoff_delay(2, policy) == 2.0
    assert backoff_delay(4, policy) == 8.0
    assert backoff_delay(9, policy) == 10.0  # capped
    assert backoff_delay(1, policy, retry_after_s=7.0) == 7.0
    # Jitter stays inside the configured band.
    jittered = RetryPolicy(base_delay_s=4.0, max_delay_s=10.0, jitter_ratio=0.5)
    for _ in range(50):
        value = backoff_delay(1, jittered)
        assert 2.0 <= value <= 6.0


def test_retry_after_header_parsing_is_bounded():
    assert parse_retry_after_header("3") == 3.0
    assert parse_retry_after_header(" 2.5 ") == 2.5
    assert parse_retry_after_header("-1") is None
    assert parse_retry_after_header("9999") is None
    assert parse_retry_after_header("Tue, 15 Nov 1994 08:12:31 GMT") is None
    assert parse_retry_after_header(None) is None
    assert retry_after_seconds(ProviderRateLimitError("x", retry_after_s=2.0)) == 2.0


def test_failure_classification_covers_transient_and_permanent_cases():
    assert classify_provider_failure(ProviderRateLimitError("r")) is FailureKind.RATE_LIMIT
    assert classify_provider_failure(ProviderServerError("s")) is FailureKind.SERVER
    assert classify_provider_failure(ProviderAuthError("a")) is FailureKind.AUTH
    assert classify_provider_failure(ProviderRequestError("q")) is FailureKind.REQUEST
    assert classify_provider_failure(ProviderTimeoutError("t")) is FailureKind.TIMEOUT
    assert classify_provider_failure(asyncio.TimeoutError()) is FailureKind.TIMEOUT
    assert classify_provider_failure(ConnectionResetError()) is FailureKind.CONNECT
    # A truncated read / broken stream is a connection-level failure and stays retryable.
    request = httpx.Request("POST", "https://x.invalid")
    assert classify_provider_failure(httpx.ReadError("truncated", request=request)) is FailureKind.CONNECT
    assert classify_provider_failure(httpx.RemoteProtocolError("gone", request=request)) is FailureKind.CONNECT
    assert classify_provider_failure(httpx.ReadTimeout("slow", request=request)) is FailureKind.TIMEOUT
    assert classify_provider_failure(httpx.PoolTimeout("busy", request=request)) is FailureKind.TIMEOUT
    response = httpx.Response(503, request=request)
    http_error = httpx.HTTPStatusError("boom", request=request, response=response)
    assert classify_provider_failure(http_error) is FailureKind.SERVER
    assert classify_provider_failure(ValueError("nope")) is FailureKind.UNKNOWN


def test_circuit_breaker_opens_half_opens_and_closes():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=5.0, clock=lambda: now[0])
    assert breaker.state.value == "CLOSED"
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state.value == "CLOSED"
    breaker.record_failure()
    assert breaker.state.value == "OPEN"
    assert not breaker.allow()
    assert breaker.snapshot()["rejected_while_open"] == 1
    now[0] = 5.0
    assert breaker.state.value == "HALF_OPEN"
    assert breaker.allow()          # single probe allowed
    assert not breaker.allow()      # second concurrent probe refused
    breaker.record_success()
    assert breaker.state.value == "CLOSED"
    assert breaker.allow()


# --------------------------------------------------------------------------- #
# Reliable adapter
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reliability_retries_transient_faults_and_reports_metrics():
    delegate = StubAdapter([
        ProviderRateLimitError("limited", retry_after_s=2.0),
        ProviderServerError("boom"),
        ProviderTimeoutError("timeout"),
        {"response_text": "ok"},
    ])
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    metrics = ReliabilityMetrics()
    policy = RetryPolicy(max_attempts=4, base_delay_s=1.0, max_delay_s=8.0, jitter_ratio=0.0)
    adapter = ReliableLLMAdapter(delegate, policy=policy, metrics=metrics, sleep=fake_sleep)
    payload = await adapter.generate(probe_request())
    assert payload == {"response_text": "ok"}
    assert delegate.calls == 4
    snapshot = metrics.snapshot()
    assert snapshot["attempts"] == 4 and snapshot["retries"] == 3 and snapshot["successes"] == 1
    assert snapshot["retries_by_kind"] == {"RATE_LIMIT": 1, "SERVER": 1, "TIMEOUT": 1}
    assert snapshot["retry_after_observed_s"] == 2.0
    assert waits == [2.0, 2.0, 4.0]   # Retry-After raises the first backoff


@pytest.mark.asyncio
async def test_reliability_does_not_retry_auth_or_invalid_output():
    auth_delegate = StubAdapter([ProviderAuthError("nope")])
    metrics = ReliabilityMetrics()
    adapter = ReliableLLMAdapter(auth_delegate, policy=RetryPolicy(max_attempts=3), metrics=metrics,
                                 sleep=lambda _s: asyncio.sleep(0))
    with pytest.raises(ProviderAuthError):
        await adapter.generate(probe_request())
    assert auth_delegate.calls == 1 and metrics.snapshot()["retries"] == 0

    from continuity_engine import ProviderResponseError
    invalid_delegate = StubAdapter([ProviderResponseError("provider_chat_content_not_json")])
    metrics2 = ReliabilityMetrics()
    adapter2 = ReliableLLMAdapter(invalid_delegate, policy=RetryPolicy(max_attempts=3), metrics=metrics2,
                                  sleep=lambda _s: asyncio.sleep(0))
    with pytest.raises(ProviderResponseError):
        await adapter2.generate(probe_request())
    assert invalid_delegate.calls == 1 and metrics2.snapshot()["retries"] == 0


@pytest.mark.asyncio
async def test_reliability_never_replays_a_side_effecting_request():
    delegate = StubAdapter([ProviderRateLimitError("limited", retry_after_s=0.1), {"response_text": "ok"}])
    metrics = ReliabilityMetrics()
    adapter = ReliableLLMAdapter(delegate, policy=RetryPolicy(max_attempts=3), metrics=metrics,
                                 sleep=lambda _s: asyncio.sleep(0))
    with pytest.raises(ProviderRateLimitError):
        await adapter.generate(probe_request(replay_safe=False))
    snapshot = metrics.snapshot()
    assert delegate.calls == 1
    assert snapshot["retries"] == 0
    assert snapshot["retries_suppressed_no_replay"] == 1


@pytest.mark.asyncio
async def test_reliability_fails_fast_when_a_retry_would_miss_the_deadline():
    delegate = StubAdapter([ProviderServerError("boom"), {"response_text": "ok"}])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    metrics = ReliabilityMetrics()
    adapter = ReliableLLMAdapter(delegate, policy=RetryPolicy(max_attempts=3, base_delay_s=5.0,
                                                              max_delay_s=5.0, jitter_ratio=0.0),
                                 metrics=metrics, sleep=fake_sleep)
    with pytest.raises(ProviderServerError):
        await adapter.generate(probe_request(deadline_s=1.0))
    assert sleeps == [] and delegate.calls == 1


@pytest.mark.asyncio
async def test_reliability_opens_circuit_after_repeated_failures_and_recovers():
    delegate = StubAdapter([ProviderServerError("down"), ProviderServerError("down"),
                            {"response_text": "ok"}])
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=5.0, clock=lambda: now[0])
    metrics = ReliabilityMetrics()
    adapter = ReliableLLMAdapter(delegate, policy=RetryPolicy(max_attempts=1), breaker=breaker,
                                 metrics=metrics, clock=lambda: now[0],
                                 sleep=lambda _s: asyncio.sleep(0))
    with pytest.raises(ProviderServerError):
        await adapter.generate(probe_request())
    with pytest.raises(ProviderServerError):
        await adapter.generate(probe_request())
    assert breaker.state.value == "OPEN"
    with pytest.raises(CircuitOpenError):
        await adapter.generate(probe_request())
    now[0] = 5.0
    payload = await adapter.generate(probe_request())
    assert payload == {"response_text": "ok"}
    assert breaker.state.value == "CLOSED"
    assert metrics.snapshot()["circuit_rejections"] == 1


# --------------------------------------------------------------------------- #
# Adapter transport behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("status,retry_after,expected", [
    (429, "2", ProviderRateLimitError),
    (401, None, ProviderAuthError),
    (403, None, ProviderAuthError),
    (500, None, ProviderServerError),
    (503, None, ProviderServerError),
    (400, None, ProviderRequestError),
])
async def test_adapter_maps_http_status_to_typed_errors(status, retry_after, expected):
    async def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"retry-after": retry_after} if retry_after else {}
        return httpx.Response(status, headers=headers, text="upstream error body")

    adapter, client = mock_adapter(handler)
    try:
        with pytest.raises(expected) as info:
            await adapter.generate(probe_request())
        if retry_after:
            assert info.value.retry_after_s == 2.0
        assert info.value.status_code == status
        assert "upstream error body" not in str(info.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_adapter_sends_thinking_disabled_and_extra_body():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"response_text":"hi"}'}}]})

    adapter, client = mock_adapter(handler, disable_thinking=True, extra_body={"top_p": 0.9})
    try:
        await adapter.generate(probe_request())
    finally:
        await client.aclose()
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert seen["body"]["top_p"] == 0.9
    assert seen["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_adapter_streams_sse_and_aggregates_deltas():
    def sse(obj) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    parts = ['{"res', 'ponse_text"', ': "hi"}']
    body = ("".join(sse({"choices": [{"delta": {"content": part}}]}) for part in parts)
            + sse({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 4,
                                            "total_tokens": 15, "model": "test-model"}})
            + "data: [DONE]\n\n").encode("utf-8")
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    deltas: list[str] = []
    adapter, client = mock_adapter(handler, stream=True, on_delta=lambda text: deltas.append(text))
    try:
        payload = await adapter.generate(probe_request())
    finally:
        await client.aclose()
    assert seen["body"]["stream"] is True
    assert payload == {"response_text": "hi"}
    assert deltas == parts
    usage = adapter.usage_snapshot()
    assert usage["completion_tokens"] == 4 and usage["responses"] == 1


@pytest.mark.asyncio
async def test_adapter_records_usage_without_retaining_text():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "test-model",
            "choices": [{"message": {"content": '{"response_text":"secret reply"}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
                      "completion_tokens_details": {"reasoning_tokens": 0}},
        })

    adapter, client = mock_adapter(handler)
    try:
        await adapter.generate(probe_request())
    finally:
        await client.aclose()
    usage = adapter.usage_snapshot()
    assert usage["prompt_tokens"] == 20 and usage["completion_tokens"] == 5
    assert usage["reasoning_tokens"] == 0
    assert usage["last"]["model"] == "test-model"
    assert "secret" not in json.dumps(usage)


def test_adapter_configures_connection_pool_and_granular_timeouts():
    adapter = OpenAICompatibleAdapter(endpoint_url="https://provider.invalid/v1/chat/completions",
                                      model="m", timeout_s=30, connect_timeout_s=3, read_timeout_s=20,
                                      max_connections=8, max_keepalive_connections=4)
    try:
        assert adapter.max_connections == 8 and adapter.max_keepalive_connections == 4
        assert adapter.connect_timeout_s == 3 and adapter.read_timeout_s == 20
    finally:
        asyncio.run(adapter.aclose())
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleAdapter(endpoint_url="https://provider.invalid/v1/chat", model="m",
                                max_connections=2, max_keepalive_connections=5)
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleAdapter(endpoint_url="https://provider.invalid/v1/chat", model="m", extra_body=[1])


def test_adapter_from_file_accepts_new_transport_keys(tmp_path):
    path = tmp_path / "provider.yaml"
    path.write_text("provider:\n  endpoint_url: https://provider.invalid/v1/chat/completions\n"
                    "  model: m\n  disable_thinking: true\n  stream: true\n"
                    "  max_connections: 6\n  max_keepalive_connections: 3\n", encoding="utf-8")
    adapter = OpenAICompatibleAdapter.from_file(path)
    try:
        assert adapter.disable_thinking and adapter.stream
        assert adapter.max_connections == 6 and adapter.max_keepalive_connections == 3
        assert adapter.extra_body["thinking"] == {"type": "disabled"}
    finally:
        asyncio.run(adapter.aclose())


# --------------------------------------------------------------------------- #
# Gateway integration
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gateway_output_repair_is_skipped_for_non_replay_safe_requests(config_manager):
    engine = ContinuityEngine(config_manager)
    try:
        view = engine.state_view_builder.build("simple_response", engine.state, user_input="hi")
        delegate = StubAdapter([{"response_text": ""}, {"response_text": ""}, {"response_text": ""}])
        gateway = LLMGateway(delegate, timeout_s=5, max_output_retries=2)
        result = await gateway.generate(interaction_id="i", intent_id=None, generation=1,
                                        purpose="simple_response", view=view, replay_safe=False)
        assert result.status is LLMStatus.REJECTED
        assert result.error_code == "INVALID_RESPONSE_TEXT"
        assert delegate.calls == 1
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_gateway_still_repairs_malformed_output_for_replay_safe_requests(config_manager):
    engine = ContinuityEngine(config_manager)
    try:
        view = engine.state_view_builder.build("simple_response", engine.state, user_input="hi")
        delegate = StubAdapter([{"response_text": ""}, {"response_text": "fixed"}])
        gateway = LLMGateway(delegate, timeout_s=5, max_output_retries=2)
        result = await gateway.generate(interaction_id="i", intent_id=None, generation=1,
                                        purpose="simple_response", view=view)
        assert result.status is LLMStatus.COMPLETED
        assert result.payload["response_text"] == "fixed"
        assert delegate.calls == 2
    finally:
        engine.close()


# --------------------------------------------------------------------------- #
# Drill harness
# --------------------------------------------------------------------------- #
def test_fault_injecting_adapter_is_deterministic_and_counts_calls():
    delegate = StubAdapter([{"response_text": "ok"}])
    adapter = FaultInjectingAdapter(delegate, faults=[
        FaultSpec(FAULT_RATE_LIMIT, retry_after_s=1.0),
        FaultSpec(FAULT_SERVER, count=2, status_code=502),
    ])
    assert len(expand_faults([FaultSpec(FAULT_SERVER, count=3)])) == 3

    async def run():
        with pytest.raises(ProviderRateLimitError):
            await adapter.generate(probe_request())
        with pytest.raises(ProviderServerError):
            await adapter.generate(probe_request())
        with pytest.raises(ProviderServerError):
            await adapter.generate(probe_request())
        return await adapter.generate(probe_request())

    payload = asyncio.run(run())
    assert payload == {"response_text": "ok"}
    assert adapter.calls == 4 and adapter.pending_faults == 0
    assert adapter.injected == [FAULT_RATE_LIMIT, FAULT_SERVER, FAULT_SERVER]
    assert delegate.calls == 1


def test_fault_injecting_adapter_outage_always_fails_then_clears():
    delegate = StubAdapter([{"response_text": "ok"}])
    adapter = FaultInjectingAdapter(delegate, outage=True)

    async def run():
        with pytest.raises(ProviderServerError):
            await adapter.generate(probe_request())
        adapter.set_outage(False)
        return await adapter.generate(probe_request())

    assert asyncio.run(run()) == {"response_text": "ok"}
    assert adapter.snapshot()["injected"] == ["OUTAGE"]


def test_resource_sampler_summarizes_without_payload_data():
    async def run():
        sampler = ResourceSampler(interval_s=0.05, loop_task_counter=lambda: 3)
        await sampler.start()
        sampler.sample_once()
        await asyncio.sleep(0.12)
        await sampler.stop()
        return sampler.summary()

    summary = asyncio.run(run())
    assert summary["sample_count"] >= 2
    assert summary["threads"]["min"] >= 1
    assert summary["asyncio_tasks"]["max"] == 3
    assert "no raw" not in json.dumps(summary)  # only aggregate numbers are present


def test_drill_config_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        DrillConfig(turns=5)
    with pytest.raises(ValueError):
        DrillConfig(turns=10, memory_expected_turn_index=8, context_expected_turn_index=12)
    with pytest.raises(ValueError):
        DrillConfig(circuit_failure_threshold=0)
    with pytest.raises(ValueError):
        DrillConfig(compat_attempts=0)
    with pytest.raises(ValueError):
        DrillConfig(deadline_timeout_s=0)


def test_conversation_script_places_long_range_probes():
    runner = ProviderDrillRunner(environment=object(), config=DrillConfig(turns=20))
    script = runner._conversation_script()
    assert len(script) == 20
    labels = {checks.get("label"): index for index, (_text, checks) in enumerate(script)}
    assert labels["memory_recall"] == 4
    assert labels["context_continuation"] == 12
    # The context statement must be spoken before the long-range question.
    leads = [index for index, (text, _c) in enumerate(script)
             if text == runner.config.context_statement]
    assert leads and leads[0] < labels["context_continuation"]
    assert script[0][0] == runner.config.memory_statement


class _FakeTurn:
    def __init__(self, status, text="ok answer", degraded=False, error_code=None):
        self.status = status
        self.response_text = text
        self.degraded = degraded
        self.error_code = error_code


class _FakeCompanion:
    """Deterministic companion keyed on the authored turn, not on raw call order.

    Keying on text keeps the assertions stable even though labelled probes consume
    extra calls when they retry.
    """

    def __init__(self, config, *, filler=(), memory=(), context=()):
        self.config = config
        self.filler = list(filler)
        self.memory = list(memory)
        self.context = list(context)
        self.turns = 0

    async def chat(self, text, *, interaction_id="default"):
        self.turns += 1
        if text == self.config.memory_question:
            status = self.memory.pop(0) if self.memory else "COMPLETED"
            response = self.config.memory_expected_tokens[0]
        elif text == self.config.context_question:
            status = self.context.pop(0) if self.context else "COMPLETED"
            response = self.config.context_expected_tokens[0]
        else:
            status = self.filler.pop(0) if self.filler else "COMPLETED"
            response = "ok answer"
        degraded = status in {"DEGRADED", "SILENCED"}
        if degraded:
            response = "本地降级回复"
        return _FakeTurn(status, text=response, degraded=degraded,
                         error_code="DEGRADED_LOCAL_FALLBACK" if degraded else None)

    def history(self, interaction_id="default"):
        return [{"role": "user", "content": "x"}] * (self.turns * 2)


@pytest.mark.asyncio
async def test_conversation_counts_disclosed_degradation_separately_from_hard_failures():
    config = DrillConfig(turns=20)
    incident = _FakeCompanion(config, filler=["COMPLETED"] * 14 + ["DEGRADED", "COMPLETED", "FAILED"])
    runner = ProviderDrillRunner(environment=object(), config=config)
    result = await runner._run_conversation(type("S", (), {"companion": incident})(), run_id="unit")
    assert result["turn_count"] == 20
    assert result["degraded"] == 1
    assert result["failed"] == 1
    assert result["degradation_rate"] == 0.05
    assert result["error_rate"] == 0.05
    assert result["memory_recall_ok"] is True and result["context_continuation_ok"] is True
    assert runner._verdicts((), result, [], {}, {}, {})["long_conversation_no_hard_failures"] is False

    runner2 = ProviderDrillRunner(environment=object(), config=config)
    clean = await runner2._run_conversation(
        type("S", (), {"companion": _FakeCompanion(config)})(), run_id="unit2")
    assert clean["failed"] == 0 and clean["degraded"] == 0
    assert runner2._verdicts((), clean, [], {}, {}, {})["long_conversation_no_hard_failures"] is True


@pytest.mark.asyncio
async def test_labelled_probe_retries_within_its_budget_then_reports_attempts():
    config = DrillConfig(turns=20, probe_turn_attempts=3)
    # The context probe degrades twice then succeeds: the capability is still verified.
    companion = _FakeCompanion(config, context=["DEGRADED", "DEGRADED", "COMPLETED"])
    runner = ProviderDrillRunner(environment=object(), config=config)
    result = await runner._run_conversation(type("S", (), {"companion": companion})(), run_id="unit3")
    assert result["context_continuation_ok"] is True
    probe = next(row for row in result["per_turn"] if row.get("label") == "context_continuation")
    assert probe["attempts"] == 3 and probe["passed"] is True

    # Exhausting the budget is reported, not hidden.
    config2 = DrillConfig(turns=20, probe_turn_attempts=2)
    companion2 = _FakeCompanion(config2, context=["DEGRADED", "DEGRADED"])
    runner2 = ProviderDrillRunner(environment=object(), config=config2)
    result2 = await runner2._run_conversation(type("S", (), {"companion": companion2})(), run_id="unit4")
    assert result2["context_continuation_ok"] is False
    assert any("context_continuation" in note for note in runner2.notes)


def test_drill_config_rejects_invalid_retry_budget_and_inflight_window():
    with pytest.raises(ValueError):
        DrillConfig(restart_attempts=0)
    with pytest.raises(ValueError):
        DrillConfig(outage_recovery_attempts=11)
    with pytest.raises(ValueError):
        DrillConfig(cancellation_inflight_s=0)


def test_cancellation_verdict_exposes_an_inflight_race():
    runner = ProviderDrillRunner(environment=object(), config=DrillConfig())
    raced = {"no_leaked_tasks": True, "control_interrupted": True,
             "request_inflight_when_cancelled": False, "control_request_inflight_when_stopped": True}
    verdicts = runner._verdicts((), {}, [], raced, {}, {})
    assert verdicts["cancellation_was_inflight"] is False
    assert verdicts["cancellation_no_leaked_tasks"] is True


def test_drill_report_renders_markdown_and_omits_raw_content():
    report = DrillReport(
        schema_version="1.0", run_id="run-1", started_at=0.0, completed_at=1.0,
        provider={"vendor": "unit-test", "model": "stub"}, config={"turns": 10},
        compatibility=({"purpose": "simple_response", "status": "COMPLETED", "contract_ok": True,
                        "samples": 1, "violations": 0, "latency_s": 0.5, "first_error_code": None},),
        conversation={"turn_count": 10, "completed": 10, "degraded": 0, "failed": 0, "pass_rate": 1.0,
                      "error_rate": 0.0, "latency_p50_s": 0.5, "latency_p95_s": 1.0, "latency_max_s": 1.0,
                      "memory_recall_ok": True, "context_continuation_ok": True, "no_fabrication_ok": True,
                      "per_turn": [{"index": 0, "status": "COMPLETED", "passed": True, "latency_s": 0.5,
                                    "response_chars": 12, "error_code": None}]},
        transport_drills=({"name": "x", "passed": True, "observed": {"a": 1}},),
        cancellation={"no_leaked_tasks": True}, restart={"context_restored": True},
        outage_recovery={"recovered": True}, reliability={"transport_drills": {"retries": 3}},
        companion_metrics={"statuses": {"COMPLETED": 10}}, resources={"sample_count": 4},
        verdicts={"output_contract_all_purposes": True}, token_usage={"total_tokens": 123},
        notes=("unit test",))
    assert report.passed
    payload = report.to_dict()
    assert payload["passed"] is True and payload["token_usage"]["total_tokens"] == 123
    assert payload["privacy"].startswith("statuses, timings")
    markdown = report.to_markdown()
    for heading in ("# Provider drill report", "## Verdicts", "## Output-contract compatibility",
                    "## Conversation continuity", "## Failure drills", "## Resource usage"):
        assert heading in markdown


def test_drill_report_write_creates_json_and_markdown(tmp_path):
    report = DrillReport(
        schema_version="1.0", run_id="run-2", started_at=0.0, completed_at=1.0,
        provider={}, config={}, compatibility=(), conversation={}, transport_drills=(),
        cancellation={}, restart={}, outage_recovery={}, reliability={}, companion_metrics={},
        resources={}, verdicts={"only": True})
    paths = report.write(tmp_path / "reports" / "drill")
    assert set(paths) == {"json", "markdown"}
    written = json.loads((tmp_path / "reports" / "drill.json").read_text(encoding="utf-8"))
    assert written["passed"] is True
    assert math.isclose(written["duration_s"], 1.0)


@pytest.mark.asyncio
async def test_reliable_adapter_forwards_close_to_delegate():
    delegate = StubAdapter([{"response_text": "ok"}])
    adapter = ReliableLLMAdapter(delegate)
    await adapter.generate(probe_request())
    await adapter.aclose()
    assert delegate.closed is True
