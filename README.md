# Continuity Engine — executable MVP scaffold

[English](README.md) | [中文](README_zh.md)

This repository contains the first executable implementation of the Continuity Engine v0.2 engineering baseline. It is a **deterministic core + explicit async adapters**, not a finished robot product.

## What is implemented

- Versioned `Event` data model with JSON-compatible payload validation.
- Bounded event router with a protected control lane, fast-control latches, deduplication, coalescing and explicit overload reporting.
- Conservative evidence-based world model with `CONFIRMED`, `PROBABLE`, `UNKNOWN`, `STALE` and `CONFLICT` states.
- Bounded drive dynamics with exact first-order updates.
- Intent manager with score margin, dwell time, consecutive confirmation, urgency override and two-round clarification cap.
- Action proposals, local safety filtering, idempotent simulated action executor and terminal receipts.
- Purpose-specific, read-only state views for LLM requests; arbitrary raw snapshot serialization is not exposed.
- LLM gateway abstraction with bounded concurrency, timeouts, schema checks, stale-result handling and child-worker cancellation.
- LLM-backed dialogue bridge with interaction generations, filtered context, and stale-response rejection before speech proposals.
- Explicit event-schema migration and SQLite snapshots/event/memory persistence.
- Memory lineage deletion with durable pending prediction rebuild jobs and a fail-closed model reconciliation API.
- Replay mode that requires fixture LLM and simulated executor adapters and blocks common Python socket APIs while active.
- Deterministic tests and a runnable end-to-end demo.

## What is intentionally not implemented

- A real cloud/local LLM provider adapter.
- Real microphone, camera, motor, speaker, or ROS integration.
- Physical safety certification or guaranteed hardware emergency stop.
- Production identity recognition or calibrated multi-sensor Bayesian fusion.
- A full production-grade OS sandbox. Replay socket monkeypatching is defense-in-depth; use process/container-level network egress restrictions too.
- A production privacy policy for a specific jurisdiction or a complete deletion pipeline across third-party backups.
- A background scheduler service. The host application calls `step()` on a clock and handles the returned proposals; external I/O stays outside `step()`.

Those are explicit adapter/product integration tasks, not mocked as already working.

## Requirements

- Python 3.11+
- PyYAML
- pytest (development only)

## Install and test

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[test]"
pytest -q
```

## Run the demo

```bash
python examples/demo.py
```

The demo uses `ReplayClock`, `ReplayActionExecutor`, and replay-mode isolation. It does not call a real LLM, network service, or physical hardware.

## Minimal integration patterns

Local fallback path (no provider required):

```python
from continuity_engine import ConfigManager, ContinuityEngine, EventKind

config = ConfigManager.from_yaml("config/default.yaml")
engine = ContinuityEngine(config)  # simulated executor by default
try:
    engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "你好"}, source="mic"))
    step = engine.step(dt=0.0)
    for proposal in step.proposals:
        # In a host app, dispatch asynchronously and handle ActionResult / terminal receipt.
        ...
finally:
    engine.close()
```

LLM-backed dialogue uses a per-conversation generation and an explicit action-ready event:

```python
message = engine.new_event(
    EventKind.USER_MESSAGE.value,
    {"text": "你好", "interaction_id": "chat-1"}, source="mic",
)
engine.inject(message)
step = engine.step(dt=0.0)
intent = step.state.current_intent  # WAITING / awaiting_llm; no audio is spoken yet
if intent is not None:
    generation = engine.current_generation("chat-1")
    result = await engine.request_llm(
        purpose="simple_response", interaction_id="chat-1", generation=generation,
        user_input="你好", intent_id=intent.intent_id,
    )
    if result.status.value == "COMPLETED":
        accepted = engine.submit_llm_response(
            intent_id=intent.intent_id, interaction_id="chat-1", generation=generation,
            response_text=result.payload["response_text"],
        )
        if accepted:
            step = engine.step(dt=0.0)  # returns a SPEAK proposal for the validated response
```

The host application must then dispatch the resulting proposal asynchronously. The example assumes
an already-created `LLMGateway` with a provider adapter; this repository intentionally ships no real
provider client.

In production, use an explicit host scheduler, handle cancellation IDs, persist state on orderly shutdown, and do not let a real actuator bypass the `SafetySupervisor` and hardware safety controller.

## Replay guarantee and its limit

Replay mode rejects adapters unless the concrete `ReplayLLMAdapter` and `ReplayActionExecutor` types are used. It also monkeypatches common socket APIs for the lifetime of the replay engine. This is suitable for tests, but **not a security boundary against arbitrary native extensions or a malicious process**. Run replay in a network-isolated test process/container for stronger guarantees.

## Development commands

```bash
python -m compileall -q src
PYTHONPATH=src pytest -q
```

See `docs/` for the v0.2 product constraints, architecture decisions, engine contracts, LLM integration and acceptance test baseline. Numeric targets are initial hypotheses unless explicitly tested in code. See `TEST_REPORT.md` for the current local verification results and known gaps.
