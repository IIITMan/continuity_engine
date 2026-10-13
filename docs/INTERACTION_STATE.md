# Interaction State (v0.11.0-dev)

## Decision

This version does **not** implement an independent main short-term-goal system. There are no candidate-goal objects, goal priorities, activation arbitration, goal replacement, or goal-destruction lifecycle.

Instead, `InteractionState` provides two bounded kinds of state:

- **Interaction signals**: small semantic observations such as `user.fatigue = likely`, each with confidence, source, and a TTL. Same-name observations replace earlier ones; signals expire; at most 32 are retained. Raw user messages are not stored.
- **Current interaction strategy**: an optional host-approved conversational strategy, such as `倾听优先 / 以倾听为主，减少追问。` There is at most one current strategy, but it is a state field—not a goal instance. The host explicitly sets or clears it. An optional TTL can be applied.

A signal does not automatically activate or change a strategy. LLM interpretations are evidence for the host's decision, not authorization to mutate strategy state.

## API

```python
# Record a semantic signal, not a verbatim user utterance.
engine.observe_interaction_signal(
    "user.fatigue", "likely", confidence=0.72, source="host", ttl_s=300
)

# Explicitly set a host-approved strategy that constrains dialogue behavior.
engine.set_interaction_strategy(
    "倾听优先",
    "以倾听为主，减少追问，不急于给出解决方案。",
    source="engine_decision",
    ttl_s=600,  # optional; omit to keep it until explicitly cleared
    reason_code="user_continues_sharing",
)

# Clear it when the host determines that it no longer applies.
engine.clear_interaction_strategy(reason_code="user_changed_topic")

snapshot = engine.interaction_state_snapshot()
```

Supported sources are `engine_decision`, `explicit_user_preference`, and `host`. The state manager validates sizes and types, but it does not decide whether a strategy is semantically appropriate. That remains the life engine/host's responsibility.

## LLM projection

For dialogue requests, `StateViewBuilder` exposes only a reviewed projection of the current strategy and up to 32 signals. The OpenAI-compatible adapter appends the host-approved current strategy to the dialogue system prompt as a binding interaction constraint. Safety, privacy, system constraints, and the user's explicit current boundary remain higher priority.

This prompt-level constraint is not a mathematical guarantee that every arbitrary natural-language response conforms to the strategy. If deterministic enforcement is required, the host needs a constrained strategy vocabulary or a separate response validation/policy layer. This first version does not claim to solve that semantic validation problem.

## Lifecycle and limits

- Signals expire by TTL and can be explicitly cleared.
- The current strategy can optionally expire by TTL or be explicitly cleared/replaced by the host.
- There is no priority comparison between strategies. Setting a new strategy directly replaces the current one because the host has already made the decision.
- Expiration only clears state; it does not automatically infer or activate a replacement.
- State is in-memory and is not persisted across process restarts in this version.
- Emotional signals are lightweight contextual cues, not validated psychological assessments.
