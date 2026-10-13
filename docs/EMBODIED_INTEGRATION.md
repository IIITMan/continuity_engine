# Embodied expression integration

`EmbodiedExpressionCoordinator` is a high-level, opt-in proposal mapper. It is not a motor controller.

```python
from continuity_engine import EmbodiedExpressionCoordinator

expressions = EmbodiedExpressionCoordinator(engine, enabled=True)
# The proposal enters the normal engine event queue and is not executed here.
decision = expressions.propose(
    "startle", intensity=0.7, sleeping=False,
    reason_code="verified_transient_sound",
)
```

Requirements:

- `life_layer.body.enabled` is true, an enabled `BodyController` is injected and a real reviewed `BodyDriver` is healthy.
- Configure explicit semantic mappings in the host. Do not map arbitrary LLM text or user text directly to a body command.
- Do not bypass `ContinuityEngine.step()`, `IntentManager`, `SafetySupervisor`, fast control latches or dispatch-time checks.
- The host must implement a physical stop path independent of the Python process when hardware risk requires it.
- `decision.accepted` means the semantic proposal was accepted by the engine queue, not that a motor or light completed an action. Terminal execution receipts are authoritative.

The bundled code deliberately contains no vendor-specific robot hardware driver. A missing driver returns a failure through `BodyController`; it must never fabricate success.
