# Continuity Engine — Acceptance Hardening v0.25.1-dev

This repository contains **Continuity Engine v0.25.1-dev**, a deterministic companion-engine core with structured demand understanding, persistent event/memory infrastructure, optional LLM dialogue adapters, and a modular LIFE_LAYER. It is a **deterministic core + explicit async adapters**, not a finished robot product, a claim of machine consciousness, or a guarantee of human-like emotion.

Ordinary user requests should route through structured demand understanding and engine-owned policy; STOP/CANCEL/QUIET controls remain on the independent deterministic control lane. See `docs/REQUEST_ROUTING.md` and `docs/LIFE_LAYER.md`.

## What is implemented

- Versioned `Event` data model with JSON-compatible payload validation.
- Bounded event router with a protected control lane, fast-control latches, deduplication, coalescing and explicit overload reporting.
- Conservative evidence-based world model with `CONFIRMED`, `PROBABLE`, `UNKNOWN`, `STALE` and `CONFLICT` states.
- Bounded drive dynamics with exact first-order updates.
- Intent manager with score margin, dwell time, consecutive confirmation, urgency override and two-round clarification cap.
- Action proposals, local safety filtering, idempotent simulated executor and terminal receipts. Optional real action executor includes platform-native speech, desktop notifications, cancellable waits, a Linux sysfs LED driver, and a sensor-provider protocol.
- Purpose-specific, read-only state views for LLM requests; arbitrary raw snapshot serialization is not exposed.
- LLM gateway abstraction with single-request-by-default concurrency, bounded timeouts, schema checks, stale-result handling and child-worker cancellation.
- LLM-backed dialogue bridge with interaction generations, filtered context, and stale-response rejection before speech proposals.
- Explicit event-schema migration and SQLite snapshots/event/memory persistence.
- Memory lineage deletion with durable pending prediction rebuild jobs and a fail-closed model reconciliation API.
- Replay mode that requires fixture LLM and simulated executor adapters, blocks non-loopback network access, and permits loopback/Unix-domain IPC needed by local event loops.
- Rule-based autonomous behavior candidates for quiet rest, observation, and gated social check-in.
- Bounded sequential task manager with action deadlines, explicit reconciliation, persisted task state, and a minimized host-side replan request after definite failure.
- LLM task-plan proposal orchestrator: purpose-specific state view, configured internal-action allowlist, preflight validation, idempotent event enqueue, cancellation, and definite-failure replan correlation.
- Optional OpenAI-compatible async HTTP adapter using strict purpose prompts and JSON output; no provider call is made by tests/demos.
- Capability-based request router covering direct controls, conversation, knowledge/current-info, time/weather, capability queries, visual observation, mobility, composite embodied tasks, reminders, timers, media/device control and general task delegation.
- Explicit capability registry: an ability is unavailable until both the relevant feature flags and a real handler are injected; unsupported requests do not fabricate execution.
- v0.9 local persistent reminder scheduler: SQLite-backed reminders, background due-time delivery through native desktop notification, list/cancel operations, timezone-aware time parsing, and fail-closed recovery to `UNKNOWN` after a crash during delivery.
- Opt-in Open-Meteo current weather and forecast provider with location geocoding, time/source metadata and bounded timeouts.
- Opt-in Wikipedia MediaWiki search provider with source URL, bounded extracts, TTL cache and explicit unavailable/not-found/error results.
- Constrained asynchronous intent resolution for ambiguous language, bounded clarification, protected-room confirmation, and live pre-dispatch guard requirements for high-risk providers.
- Deterministic tests and runnable end-to-end demos, including `examples/request_router_demo.py`.


## LIFE_LAYER (v0.13–v0.16)

The current development line adds deterministic organs for drive-field inspection, will/intent candidates, evidence-gated attachment signals, bounded structured life-stream summaries, constrained temperament drift, an analytic breath oscillator, expression formatting, and an opt-in semantic body-driver protocol. Existing `IntentManager`, `SafetySupervisor`, cancellation controls, action allowlists, and the current drive system remain authoritative. Full details and limitations are in [`docs/LIFE_LAYER.md`](docs/LIFE_LAYER.md).

Important boundaries: (1) silence does not imply user absence or negative relationship emotion; (2) explicitly cancelled intentions do not auto-resume; (3) physical body control is disabled unless a reviewed driver is explicitly injected; (4) LLM emotion assessments are confidence-gated estimates used only for transient expression state; (5) local fallback replies explicitly disclose degraded operation and are suppressed under quiet/mute/safety-stop/user-speaking states.

The life stream stores only bounded structured events, not raw dialogue. The SQLite memory API supports retrieval/update/deletion with source lineage; applications must explicitly attach a repository and decide which user-approved memories to store. This is an integration foundation, not an automatic complete autobiographical memory service.

## Companion continuity (v0.17–v0.20)

This development line connects the v0.16 life layer to a practical companion conversation path:

- `CompanionService` orchestrates bounded multi-turn text dialogue, filtered memory recall, generation tracking, control-lane handling, speech-segment preparation and safe fallback behavior.
- `MemoryLifecycle` distinguishes user-explicit saves from heuristic candidates. Candidates remain pending until host/user approval; hybrid lexical/vector recall is available when a repository is attached. The default feature-hash encoder is a deterministic fallback; an optional local SentenceTransformers model can be injected for trained semantic embeddings. Credential-like explicit memory requests are rejected before model submission.
- `EmotionContinuity` holds a bounded, volatile affect estimate. Unknown or low-confidence signals are ignored; silence decays toward neutral and does not imply rejection or loneliness.
- `VoiceConversationPipeline` accepts audio buffers from a host, optionally runs keyword/VAD adapters, calls ASR and the same companion service, then sends bounded text segments to TTS. Cross-modal barge-in and stop/mute controls can cancel host-managed playback.
- `JsonCommandSpeechRecognizer` and `JsonCommandTextToSpeech` provide bounded JSON-over-stdin adapters for externally installed programs without invoking a shell. They do not bundle ASR/TTS models or capture devices.
- `EmbodiedExpressionCoordinator` maps a reviewed semantic vocabulary to body proposals through the engine's existing action-intent and safety path. It does not call hardware directly.

See [`docs/ROADMAP_V0_17_TO_V0_20.md`](docs/ROADMAP_V0_17_TO_V0_20.md), [`docs/VOICE_INTEGRATION.md`](docs/VOICE_INTEGRATION.md), and [`docs/EMBODIED_INTEGRATION.md`](docs/EMBODIED_INTEGRATION.md). A host can use the text loop as follows:

```python
from continuity_engine import CompanionService

companion = CompanionService(engine)
result = await companion.chat("我今天有点累", interaction_id="owner")
if result.speak_allowed:
    render_text(result.response_text)
```

The host still owns the engine step loop, UI rendering, audio capture permissions and device lifecycle. This is an integration-ready development core, not a bundled voice product or a robot with certified physical safety.


## Reality integration, evaluation & acceptance hardening (v0.21–v0.25)

- `LLMRuntime` explicitly attaches a configured OpenAI-compatible HTTP provider to a non-replay engine, distinguishes configuration status from an optional live inference probe, and supports clean shutdown.
- `CompanionMetrics` tracks bounded status/error/latency and approximate variable-context token metrics without storing raw prompts, turns, audio or credentials.
- `DialogueContextBudgeter` caps prior dialogue and retrieved memories; the current user input is not duplicated in history. Estimates are approximate and do not replace provider-reported token counts.
- `SQLiteConversationStore` provides optional bounded cross-restart recent dialogue. Persistence is **disabled unless explicitly enabled**. It supports an injected authenticated cipher and optional `FernetTranscriptCipher`; on POSIX, database/WAL/SHM files receive best-effort owner-only permissions. Plaintext mode remains explicit.
- `HybridMemoryRetriever` adds lexical + vector + recency ranking, bulk model-scoped vector loading, bounded cold-cache embedding, offline indexing, explicit correction by memory ID, and vector invalidation on update/deletion. The offline feature-hash model is not equivalent to a trained semantic encoder.
- Optional `SentenceTransformerEmbedder` integration is available with `pip install 'continuity-engine[semantic]'`; the host must select and provision a suitable local model. Optional transcript encryption is available via `pip install 'continuity-engine[crypto]'`; the encryption key remains host-managed.
- `OpenAICompatibleSpeechRecognizer` and `OpenAICompatibleTextToSpeech` connect to compatible HTTP ASR/TTS endpoints. TTS requires an explicitly injected `AudioSink`; `SubprocessAudioSink` uses a configured command without a shell. Microphone capture, permissions, and actual sound hardware remain host-owned.
- `FieldValidationRunner` creates non-destructive preflight reports and accepts host-provided physical probes. `CompanionEvaluationRunner` measures scenario pass rates and latency while omitting raw prompt/response content from reports.

See [`docs/ROADMAP_V0_21_TO_V0_24.md`](docs/ROADMAP_V0_21_TO_V0_24.md), [`docs/REAL_PROVIDER.md`](docs/REAL_PROVIDER.md), [`docs/SEMANTIC_MEMORY.md`](docs/SEMANTIC_MEMORY.md), [`docs/FIELD_VALIDATION.md`](docs/FIELD_VALIDATION.md), and [`docs/ACCEPTANCE_OPTIMIZATIONS.md`](docs/ACCEPTANCE_OPTIMIZATIONS.md). These adapters and reports do not mean a live model endpoint, microphone, speaker, or actuator has been field-tested by the repository test suite.


## What is intentionally not implemented

- Vendor certification for all commercial models/accounts/regions. A generic OpenAI-compatible adapter is provided; live acceptance evidence is model/account/host-specific and must be rerun after changes.
- Real microphone/camera capture and arbitrary motor/ROS integration. ASR/TTS HTTP adapters are provided, but microphone ownership, permissions, capture-loop endpointing, codec handling and sound output device selection remain host responsibilities. OBSERVE and LIGHT require explicit sensor/LED adapters. No general movement action is implemented because the first product profile is a static desktop companion.
- Physical safety certification or guaranteed hardware emergency stop.
- Production identity recognition or calibrated multi-sensor Bayesian fusion.
- A full production-grade OS sandbox. Replay socket monkeypatching is defense-in-depth; use process/container-level network egress restrictions too.
- A production privacy policy for a specific jurisdiction or a complete deletion pipeline across third-party backups. Recent transcript persistence is opt-in; optional Fernet transcript encryption is available when the host injects a protected key. The long-term memory/event database is not application-level encrypted by this release; use an encrypted OS volume or separately reviewed database backend for sensitive deployment data.
- Production service supervision, OS notification delivery guarantees, and hardware-specific sensor/mobility integrations. The v0.9 reminder scheduler is implemented as a host-managed async service; exactly-once delivery across process crashes cannot be guaranteed for OS notifications. A reminder interrupted while `FIRING` becomes `UNKNOWN` and is not automatically duplicated.

Those are explicit adapter/product integration tasks, not mocked as already working.

## Interaction state (contract introduced in v0.11.0-dev)

The engine no longer maintains an independent short-term-goal manager. Bounded interaction signals and an optional host-approved current conversational strategy are maintained as interaction state; signals never activate strategies automatically. See [`docs/INTERACTION_STATE.md`](docs/INTERACTION_STATE.md) for the contract and limitations.

## Request routing across interaction types

`RequestRouter` is an extensible first-pass intent/capability layer, not just a time/weather router. It distinguishes direct controls, ordinary conversation, knowledge/current-info requests, time/weather, capability-status questions, user/object/scene/location observation, mobility, composite navigate-and-observe requests, reminders, timers, media/device control, and general task delegation. Providers are opt-in, and a request that lacks a real provider is reported as unavailable rather than simulated as successful.

Examples covered by `examples/request_router_demo.py`:

- `你能看见我吗` requests a fresh `vision.observe_user` check when a camera provider is wired; without one it reports `UNSUPPORTED`. `你有摄像头吗` / `摄像头开着吗` are read-only capability/status questions and never activate the camera.
- `看看我` / `看看周围` / `识别这个物体` route to different visual capability IDs.
- `你不要打扰我好不好` becomes a local control event, not a conversational prompt to the LLM.
- `去卧室看看有没有人` is a sequential mobility+vision request and is rejected unless both providers, an approved location map and any required confirmation are present.
- `来我这里` is blocked/unsupported unless localization and an authorized mobility provider exist.
- `给我放首歌` and `帮我计时十分钟` require registered media/timer providers.

The demo intentionally has no real providers configured and makes no network/device calls. See [`docs/REQUEST_ROUTING.md`](docs/REQUEST_ROUTING.md) for the complete intent matrix, integration contract and limitations. Adding a new intent family requires adding its enum, classifier/routing rules, argument schema, safety policy and regression tests; adding a provider alone is not enough for arbitrary language to invoke it.

## Requirements

- Python 3.11+
- PyYAML
- pytest and pytest-asyncio (development only)
- httpx (optional `[provider]` extra; included in `[test]` for MockTransport tests)

## Install and test

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[test]"
pytest -q

# Optional real endpoint adapter
python -m pip install -e ".[provider]"
```

## Run the demo

```bash
python examples/demo.py
python examples/task_replan_demo.py
python examples/llm_task_plan_demo.py
python examples/real_action_executor_demo.py --help
python examples/request_router_demo.py
python examples/companion_loop_demo.py
python examples/context_budget_benchmark.py
python examples/v09_capabilities_demo.py --help
```

The request-router, companion-loop and task demos use replay/fixture adapters and simulated execution; they do not call a real LLM, network service, or physical hardware. `real_action_executor_demo.py` is an explicit opt-in smoke test: use `--speak`, `--notify`, or `--light` only when you intentionally want a real host side effect.


## v0.9 reminders, weather and knowledge providers

Install optional HTTP support and run the host-managed capability demo:

```bash
python -m pip install -e ".[test,provider]"
python examples/v09_capabilities_demo.py --reminder-db data/reminders.sqlite3
```

Reminder requests are stored in SQLite and delivered by a background scheduler using the host OS desktop-notification backend. On a desktop with a working notification backend, try `一分钟后提醒我吃饭`, `我有哪些提醒`, and `取消吃饭提醒`. Reminder delivery is a real local side effect; the demo should be run intentionally. A successful request response means the reminder was committed to the database, not that its future notification has already appeared.

Weather and knowledge network requests remain disabled by default. To opt in explicitly:

```bash
python examples/v09_capabilities_demo.py --enable-weather --enable-knowledge
```

`--enable-weather` queries Open-Meteo geocoding and forecast endpoints; `--enable-knowledge` queries the MediaWiki API for Wikipedia extracts. Network requests send the query and, for weather, the requested city/region to these providers. Configure `request_routing.default_location` only when a default city is appropriate; otherwise the router asks for a city. Knowledge responses include canonical source URLs and short retrieved extracts; they are retrieval results, not a claim that Wikipedia is exhaustive or infallible. Review the provider's current terms/attribution requirements before product distribution.

The provider bundle and demo do not enable actual camera, mobility, music, timer or smart-home integrations. Those capability IDs remain unavailable until a real approved provider and the required safety guards are injected. Network provider tests use `httpx.MockTransport`; the ordinary `pytest` suite does not contact public endpoints.

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

The host application must then dispatch the resulting proposal asynchronously. Stateful dialogue calls for one engine instance should be serialized by the host; use a per-robot turn queue, not `asyncio.gather()` over concurrent turns. `LLMGateway` defaults to one provider request at a time, but that semaphore does not replace conversation-level serialization. The repository includes an optional `OpenAICompatibleAdapter`; a real provider call is made only when an application explicitly configures and injects it. Exact endpoint/model compatibility is provider-dependent and is not asserted by local tests.

Example provider setup (configure only for an approved endpoint):

```bash
export CONTINUITY_LLM_ENDPOINT="https://YOUR-APPROVED-ENDPOINT/v1/chat/completions"
export CONTINUITY_LLM_MODEL="YOUR-MODEL-NAME"
export CONTINUITY_LLM_API_KEY="YOUR-SECRET"
```

```python
from continuity_engine import LLMGateway, OpenAICompatibleAdapter

adapter = OpenAICompatibleAdapter.from_env()
gateway = LLMGateway(adapter, timeout_s=12.0, max_concurrent=1)
# Inject gateway into ContinuityEngine, then call LLMTaskPlanner.plan_goal(...).
# On orderly shutdown, await adapter.aclose().
```

For a local YAML file, copy `config/llm.local.example.yaml` to the gitignored
`config/llm.local.yaml`, then load it with `OpenAICompatibleAdapter.from_file(...)`.
The file accepts `endpoint_url`/`endpoint`, `model`, `timeout_s`/`timeout`, `temperature`,
and either `api_key` or `api_key_env`. Prefer `api_key_env` so the secret stays in an
environment variable. Unknown keys are rejected to catch configuration typos.

Never commit the API key. Cloud use sends the purpose-filtered context view to the configured endpoint; review privacy/consent policy before enabling it.

In production, use an explicit host scheduler, handle cancellation IDs, persist state on orderly shutdown, and do not let a real actuator bypass the `SafetySupervisor` and hardware safety controller.

## Replay guarantee and its limit

Replay mode rejects adapters unless the concrete `ReplayLLMAdapter` and `ReplayActionExecutor` types are used. It monkeypatches common socket APIs for the lifetime of the replay engine. Non-loopback connects, external name resolution and non-loopback datagram/connected-socket sends are blocked; loopback TCP and Unix-domain IPC are allowed because Windows asyncio ProactorEventLoop may implement `socketpair()` with a loopback TCP connection. This is suitable for tests, but **not a security boundary against arbitrary native extensions or a malicious process**. Run replay in a network-isolated test process/container for stronger guarantees.

## Development commands

```bash
python -m compileall -q src examples tests
PYTHONPATH=src pytest -q
PYTHONPATH=src python examples/demo.py
PYTHONPATH=src python examples/task_replan_demo.py
PYTHONPATH=src python examples/llm_task_plan_demo.py
python examples/real_action_executor_demo.py --help
python examples/request_router_demo.py
python examples/v09_capabilities_demo.py --help
```

See `docs/REQUEST_ROUTING.md` for the request taxonomy and provider integration contract. See `docs/REAL_ACTION_EXECUTOR.md` for target-host setup. See `docs/` for the v0.2 product constraints, architecture decisions, engine contracts, LLM integration and acceptance test baseline. See `docs/LLM_TASK_PLANNER.md` for the LLM task-plan contract and bounded repair-on-rejection behavior, `docs/LLM_INTEGRATION.md` for the provider boundary, and `docs/AUTONOMOUS_BEHAVIOR.md` for autonomous behavior rules and `docs/TASK_MANAGER.md` for task execution/replanning contracts. Version numbers describe development increments; numeric values remain baseline hypotheses unless tested on target hardware. Numeric targets are initial hypotheses unless explicitly tested in code. See `TEST_REPORT.md` for the current local verification results and known gaps.


## Real action executor (opt-in)

The checked-in `config/default.yaml` intentionally keeps `action_execution.mode: simulated`. To opt into actual local side effects, explicitly change it to `real`, review OS permissions, and construct `action_executor_from_config(config.get(), observation_provider=..., light_driver=...)`. Pass that executor into `ContinuityEngine`; production actions must go through `engine.dispatch_action(proposal)` so the SafetySupervisor remains in the path.

`SPEAK` uses Linux `espeak-ng`/`espeak`, macOS `/usr/bin/say`, or Windows PowerShell + `System.Speech`; `NOTIFY` uses `notify-send`, `osascript`, or a Windows Forms notification. These host capabilities must exist and work in the target desktop/session. `WAIT` runs locally. `OBSERVE` requires an injected `ObservationProvider` that collects fresh evidence from the product's sensor pipeline. `LIGHT` requires an injected `LightDriver` or an explicitly configured Linux LED-class device via `SysfsLedDriver`. Missing drivers fail explicitly instead of pretending the action succeeded.

Example using an explicit real host executor (will actually speak on the host if the action is allowed):

```python
from continuity_engine import ConfigManager, ContinuityEngine, action_executor_from_config

config = ConfigManager.from_yaml("config/default.yaml")
data = config.get()
data["action_execution"]["mode"] = "real"
executor = action_executor_from_config(data)
engine = ContinuityEngine(config, action_executor=executor)
# Inject an approved intent and dispatch proposals with `await engine.dispatch_action(...)`.
# In async shutdown: `await executor.aclose()` before `engine.close()`.
```

The generic executor intentionally has no MOVE/DRIVE action, and this repo does not claim physical safety certification. A real mobile robot needs a separately engineered hardware controller and independent emergency-stop path.


## v0.8.1-dev routing fixes

- Added regression-tested quiet/stop wording variants (`闭嘴`, `安静点`, `别吵`, `别再说了`, standalone `停一下`).
- Added additional microphone capability query phrasing.
- Generalized compound navigation-and-observation recognition for `去 X 看看有什么`, with observation text passed to the vision provider.
- Added direct device-control recognition for `把灯关掉`.
- Local deterministic suite: 207 tests passed; no real provider, network or hardware was used.


## v0.11.1-dev notes

- Interaction state remains memory-only; restart restoration is intentionally not implemented.
- LLM output is checked against purpose-specific JSON contracts. Invalid output is discarded; bounded schema-repair retries are used, while task planning retains its existing bounded repair flow. See `docs/LLM_OUTPUT_CONTRACT.md`.

## Structured demand understanding (v0.12.0-dev)

New integrations can use `LLMDemandInterpreter` or `ContinuityEngine.understand_demand()` to obtain the strict `demand_understanding` schema: `request_type`, semantic `intent`, entities, a bounded emotion assessment, clarification flag, and optional response candidate. This output is interpretation only and cannot select a capability or action. The engine/host must independently apply routing and safety policy; `RequestRouter.route_understanding()` provides the engine-side mapping for this contract. The older `LLMIntentInterpreter` and fine-grained `RequestRouter.route_async()` schema remain for existing integrations during migration; see [`docs/DEMAND_UNDERSTANDING.md`](docs/DEMAND_UNDERSTANDING.md).

## Runtime-pluggable skills (v0.12.0-dev)

`SkillRegistry` provides explicit registration, permission-gated dispatch, bounded concurrency and timeout, feedback results, and guarded enable/disable/unload while executions are active. Optional voice protocols (`KeywordSpotter`, `VoiceActivityDetector`, `SpeechRecognizer`, `TextToSpeech`) are declared without importing Sherpa-ONNX. This stage provides interfaces and lifecycle primitives, not concrete microphone/ASR/TTS hardware integrations. See [`docs/SKILL_SYSTEM.md`](docs/SKILL_SYSTEM.md).

## Request processing and package layout (v0.12.0.dev1)

The canonical request entry point is `ContinuityEngine.process_user_request()`: structured LLM demand understanding followed by engine-owned policy routing. The former `LLMIntentInterpreter` / `RequestRouter.route_async()` path has been removed. Source modules are grouped under `continuity_engine/core`, `understanding`, `routing`, `state`, `llm`, `tasks`, `skills`, `execution`, and `voice`.

Skill authors should follow [`docs/SKILL_DEVELOPMENT_STANDARD.md`](docs/SKILL_DEVELOPMENT_STANDARD.md) and use [`examples/skills/echo_skill.py`](examples/skills/echo_skill.py) as the reference implementation.
