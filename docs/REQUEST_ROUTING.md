# Request routing (v0.12)

## Canonical request path

Ordinary user input must enter through `ContinuityEngine.process_user_request(...)`:

1. `LLMDemandInterpreter` interprets meaning, broad request type, entities, emotion signals, and whether clarification is needed.
2. The LLM output contract contains no capability IDs, routes, or executable instructions.
3. `RequestRouter.route_understanding(...)` applies engine-owned policy and maps the reviewed broad category to internal routing semantics.
4. The router rechecks capability availability, safety, confirmation requirements, and protected-location rules.
5. An approved decision is dispatched by the host through the appropriate capability or skill interface. Execution feedback returns to the engine; it does not automatically mutate policy or interaction state.

Example:

```python
from continuity_engine import (
    ContinuityEngine,
    LLMGateway,
    OpenAICompatibleAdapter,
    RequestRouter,
    build_default_registry,
)

# Construct config, gateway, engine and router using the application's reviewed config.
# The router has only explicitly registered and enabled capabilities.
demand, decision = await engine.process_user_request(
    user_input="今天有点累。",
    interaction_id="conversation-123",
    generation=1,
    router=router,
    request_context=request_context,
)
```

`process_user_request` returns both the validated structured demand and the policy decision. A host may also call `engine.understand_demand()` and `router.route_understanding()` separately when it needs to inspect or stage the result.

## Important boundaries

- The engine owns final routing decisions; the LLM is an interpreter, not a tool selector.
- `memory_update`, `other`, and `unclear` do not become executable operations merely because the model labels them that way.
- Capability discovery does not grant permissions. Only explicitly injected, enabled handlers are available.
- High-risk, privacy-sensitive, protected-location, or side-effecting operations continue to use confirmation and safety policy.
- The deterministic `RequestRouter.route()` method remains a low-level rule-classifier utility for deterministic controls and tests. It is not the production LLM request path.
- The former `LLMIntentInterpreter` / `route_async()` compatibility chain and its fine-grained LLM intent schema have been removed.
