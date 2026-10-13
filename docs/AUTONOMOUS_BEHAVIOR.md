# Autonomous Behavior Generator — v0.3.0-dev

## Scope

The generator turns normalized drive/world state into **candidate intents only**. It never dispatches hardware, calls an LLM, or writes arbitrary engine state. Candidates pass through `IntentManager`, then `SafetySupervisor`, and finally the host action dispatcher.

## Implemented V1 strategies

### 1. Quiet rest

When `drives.rest.value >= autonomy.rest.threshold`, the generator may propose `quiet_rest` with no external action. This makes quiet/idle a first-class policy state rather than fabricating speech. The engine ends this mode after `rest.max_duration_s` or once the rest drive falls below `rest.exit_threshold`, applies a small satisfaction impulse, and suppresses immediate re-entry for `rest.suppression_s`.

A valid user interaction can preempt quiet rest through the normal event/policy path.

### 2. Exploration / fresh observation

When the exploration drive reaches `autonomy.exploration.threshold`, the generator can ask the host to refresh one allowlisted world fact if that fact is `STALE`, `CONFLICT`, or `PROBABLE`. It proposes an `OBSERVE` action with the key and state reason. After a terminal result, it suppresses requests for that fact for `exploration.cooldown_s`; a successful action also partially satisfies the exploration drive.

This is only an observation request. Real sensor scheduling and sensor adapters remain host responsibilities.

### 3. Social check-in

A low-frequency check-in is proposed only when all of these conditions hold:

- social behavior is enabled;
- the minimum idle interval has elapsed;
- the social drive exceeds its threshold;
- `owner_present` is `CONFIRMED` or `PROBABLE`, its value is exactly `true`, and confidence meets the configured minimum;
- the user is not speaking, quiet mode is off, safety stop is not active, and no other active/waiting intent takes precedence;
- the post-interaction/proactive cooldown has elapsed.

The candidate is marked `ordinary_proactive=true`, includes an auditable reason, and must still pass the `SafetySupervisor` hourly quota/cooldown and dispatch-time recheck. Presence confidence thresholds are preliminary and require sensor calibration.

A real user interaction lowers social drive modestly and restarts the idle timer. A terminal proactive action starts a cooldown; success partially satisfies the social drive, while failures still suppress immediate retries.

## Configuration

See `config/default.yaml` under `autonomy`. These numbers are initial development hypotheses, not user-study results. `autonomy` is restart-required in v0.3; runtime hot reload is deliberately not supported for these rules yet.

## Non-goals

- No autonomous physical movement.
- No unprompted web/LLM exploration.
- No inference that a user is sad, lonely, or wants companionship merely from silence.
- No proactive speaking without sufficiently recent, supported owner-presence evidence.
- No direct execution from this module.

## Important limitations

The generator is deterministic rule-based policy, not a learned planner. It supports only a small set of conservative behaviors. It does not yet maintain a general task registry, estimate action utility from learned outcomes, or persist cooldown history across restart. Before product pilots, persist the proactive quota/suppression ledger if limits must survive restart, calibrate presence confidence, and run long-duration behavior tests on the target hardware.
