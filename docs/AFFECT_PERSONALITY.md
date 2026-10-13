# Affect and Personality (v0.9.1-dev)

## Design

Continuity Engine now separates three concerns:

- **Stable personality**: warmth, curiosity, playfulness, directness, verbosity, and initiative. Initial values are configurable and bounded to `[0, 1]`.
- **Transient affect**: valence `[-1, 1]`, arousal `[0, 1]`, and social energy `[0, 1]`. These are lightweight expression signals, not a claim that the system has human feelings. They relax toward baseline and are updated from the current message using a deterministic lexicon.
- **Expression adapter**: sends a short host-generated style instruction to the dialogue LLM. It changes wording and conversational rhythm only. It cannot change action permissions, safety rules, factual standards, quiet mode, or tool/action allowlists.

## Personality learning

The first version deliberately learns only from explicit style requests, such as “简洁点”, “详细点”, “幽默点”, “少问问题”, or “别太热情”. Each recognized preference moves one trait by at most `0.05`, clamped to `[0, 1]`. General sentiment does not change stable traits. This avoids inferring a user's enduring preferences from a single emotional message.

Stable traits are atomically saved to `data/personality_state.json` by default. Raw dialogue and affect history are not written to that file. Set `affect_personality.persistence_path: null` to disable profile persistence. The host should place the file in an appropriate user-private data directory and include it in the user's delete/reset workflow.

## API

```python
# Inspect the current profile
profile = engine.personality_snapshot()

# Apply explicit user preference feedback
profile = engine.record_personality_feedback("以后简洁点")
```

Clear style requests in user messages are also recognized automatically by the deterministic feedback rules. These rules are intentionally conservative; unsupported preferences are ignored rather than guessed.

## Current limitations

- Affect is rule-based, not a validated psychological model.
- No long-term emotional episode memory is stored.
- Personality learning is a small preference adaptation layer, not autonomous value change.
- Traits influence LLM expression only when dialogue requests use `OpenAICompatibleAdapter` and include the approved state view. Other adapters must consume `interaction_rules.expression_guidance` themselves if they want identical behavior.
- The default trait values are product starting points and should be tuned with dialogue evaluations.
