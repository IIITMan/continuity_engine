# Connecting a real LLM provider

Install the provider extra and configure an explicitly approved OpenAI-compatible endpoint. Do not commit keys in YAML or source control.

```bash
python -m pip install -e '.[provider,test]'
export CONTINUITY_LLM_ENDPOINT='https://YOUR-APPROVED-ENDPOINT/v1/chat/completions'
export CONTINUITY_LLM_MODEL='YOUR-MODEL'
export CONTINUITY_LLM_API_KEY='set-this-from-your-secret-store'
python examples/real_provider_companion.py
```

The endpoint must accept chat-completion requests and JSON-object outputs compatible with `OpenAICompatibleAdapter`. Plain HTTP is allowed only for loopback/local endpoints. Remote plain HTTP, URL-embedded credentials and redirect following are rejected/disabled.

For application embedding, attach a provider explicitly:

```python
runtime = LLMRuntime.attach(engine, env_prefix="CONTINUITY_LLM_")
companion = CompanionService(engine)
```

Call `await runtime.probe(perform_inference=True)` only when a live diagnostic request is approved; it sends a minimal prompt and can use provider quota. `await runtime.aclose()` releases the adapter. Never attach a real provider in replay mode. Provider compatibility beyond the test contract must be verified against the actual vendor/account; mock transport tests are not live endpoint verification.

## Production hardening (v0.25)

A bare adapter retries nothing: a single transport failure would end the turn. Attach the
opt-in reliability layer when the provider is a real remote service:

```python
from continuity_engine import LLMGateway, LLMRuntime, RetryPolicy, CircuitBreaker

runtime = LLMRuntime.attach(
    engine, env_prefix="CONTINUITY_LLM_",
    retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.4, max_delay_s=4.0),
    circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout_s=30.0),
)
```

This adds bounded exponential backoff with jitter, `Retry-After` honoring, automatic
circuit breaking with half-open recovery, and counters-only observability through
`runtime.reliability_snapshot()`. Two rules matter for correctness:

- **No blanket auto-replay.** A retry re-sends a request whose outcome may be unknown, so it
  is refused unless the request is marked `replay_safe`. Pure text generation is replay-safe
  by default; pass `replay_safe=False` (also accepted by `LLMGateway.generate`) for any
  request whose execution could produce an externally visible effect.
- **A retry never guarantees a missed deadline.** If the computed backoff would exceed the
  request deadline, the wrapper fails fast instead of sleeping into a timeout.

Provider thinking mode and any vendor-specific body field go through the adapter rather than
the engine: `OpenAICompatibleAdapter(..., disable_thinking=True, extra_body={...})`, or the
matching keys in a provider YAML file. `config/llm.ark.example.yaml` is a Volcano Ark /
Doubao profile.

## Live acceptance drill

Provider compatibility and failure handling should be verified against the real endpoint, not
only with mock transports:

```bash
python examples/real_provider_drill.py --turns 60 --disable-thinking --enable-reliability \
    --out data/drill_reports/ark-drill
```

The drill checks per-purpose output contracts, runs a 50-100 turn conversation with
memory-recall and context-continuation probes, exercises controlled retry / no-replay /
circuit-breaker / deadline drills, cancels in-flight work, restarts over persisted stores,
injects and ends a provider outage, and samples CPU-independent resource metrics. It writes
JSON and Markdown reports containing no prompt or response text, and exits non-zero if any
verdict failed. See [`PROVIDER_RELIABILITY.md`](PROVIDER_RELIABILITY.md) for the design and
[`PROVIDER_DRILL_REPORT.md`](PROVIDER_DRILL_REPORT.md) for a recorded live run.
