# Structured demand understanding (v0.12.0-dev)

`LLMDemandInterpreter` is the new interpretation-only contract. It returns a semantic request category, a natural-language intent summary, extracted entities, a conservative emotion assessment, a clarification flag, and an optional response candidate. It must not select a capability, route, tool, or action. The Continuity Engine remains responsible for the next decision.

## Output contract 1.0

- `schema_version`: currently `1.0`.
- `request_type`: `emotional_sharing`, `casual_chat`, `information_request`, `task_request`, `device_control`, `memory_update`, `other`, or `unclear`.
- `intent`: concise semantic description, not an executable instruction.
- `entities`: up to 32 `{type, value}` pairs.
- `emotion_assessment`: `{emotion, confidence, evidence}`. Label and confidence may be null; this is an estimate, not ground truth.
- `clarification_needed`: boolean.
- `response_candidate`: optional candidate text; it is not automatically sent to the user.

The gateway validates exact fields, bounded sizes, types, allowed request categories, and confidence ranges. Invalid output is rejected or retried only within the configured bounded retry count. The interpreter itself does not execute skills or route requests. `RequestRouter.route_understanding()` is the engine-owned policy entry point for this schema: it maps only the reviewed broad request categories to internal intent families, then reapplies registered-capability, safety, confirmation, and availability checks. Unknown, other, and memory-update categories do not become executable actions.

## Context and boundaries

The interpreter accepts host-supplied request context, personality snapshot, expression guidance, interaction-state snapshot, and optional voice signals. These are explicit projections rather than raw engine-state serialization. Current interaction strategy informs understanding but cannot grant permissions or override hard constraints. Voice signals retain their source; emotion labels emitted by an ASR model remain uncertain model outputs.

## Example

```python
from continuity_engine import LLMGateway, LLMDemandInterpreter, OpenAICompatibleAdapter

gateway = LLMGateway(OpenAICompatibleAdapter.from_env())
interpreter = LLMDemandInterpreter(gateway)
result = await interpreter.understand(
    "我今天好累",
    personality_snapshot={"traits": {"warmth": 0.8}},
    interaction_state_snapshot={"current_strategy": None, "signals": []},
)
# `result` is a validated interpretation only. The engine makes the routing decision.
# decision = request_router.route_understanding("我今天好累", result, context)
```

The former `LLMIntentInterpreter` / `RequestRouter.route_async` compatibility chain has been removed. The canonical path is `await engine.process_user_request(..., router=router)`: one structured demand-understanding call followed by engine-owned routing policy. The LLM contract intentionally contains no capability IDs, route names, or execution instructions.
