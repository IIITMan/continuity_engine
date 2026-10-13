# Real Action Executor — v0.6.0-dev

This module converts an already-approved `ActionProposal` into a real local side effect. It is not a replacement for `SafetySupervisor`: production code should normally let `ContinuityEngine.dispatch_action()` perform safety checks and enqueue the terminal receipt.

## Supported action types

| Type | Real implementation | If its driver is unavailable |
|---|---|---|
| `SPEAK` | Linux `espeak-ng` / `espeak`, macOS `say`, Windows PowerShell + `System.Speech` | `FAILED / DRIVER_UNAVAILABLE` |
| `NOTIFY` | Linux `notify-send`, macOS `osascript`, Windows Forms notification | `FAILED / DRIVER_UNAVAILABLE` |
| `WAIT` | Local cancellable asyncio delay | Always local; validation still applies |
| `OBSERVE` | Injected `ObservationProvider` callback | `FAILED / DRIVER_NOT_CONFIGURED` |
| `LIGHT` | Injected `LightDriver` or Linux sysfs LED class | `FAILED / DRIVER_NOT_CONFIGURED` |

The current product profile is a static desktop companion. No motor, wheel, joint, or `MOVE` action is implemented. A mobile robot requires a product-specific hardware adapter plus an independent physical emergency-stop path.

## Explicit opt-in

`config/default.yaml` uses `action_execution.mode: simulated` by default. This is intentional: starting the application must not unexpectedly speak, flash LEDs, or display notifications. A host application that has reviewed its device and privacy policy can change the mode to `real` and construct the adapter:

```python
from continuity_engine import ConfigManager, ContinuityEngine, action_executor_from_config

config = ConfigManager.from_yaml("config/default.yaml")
settings = config.get()
settings["action_execution"]["mode"] = "real"
executor = action_executor_from_config(settings)
engine = ContinuityEngine(config, action_executor=executor)

# Inject a validated intent/event; always dispatch via engine.dispatch_action(proposal).
# During async shutdown:
await engine.aclose()
```

The factory's `settings` controls adapter construction; `ContinuityEngine` still uses its own `ConfigManager` safety policy. Keep these configurations aligned in a real host process. For actual sensor use, inject an `ObservationProvider`; for Linux sysfs LEDs, configure a leaf `led_name` that exists under `/sys/class/leds`. Never take that LED name directly from LLM/user arguments.

## Process safety and cancellation

- Uses `asyncio.create_subprocess_exec`; there is no shell interpolation.
- Linux speech text is piped over stdin; macOS text is passed as a single argv element; Windows script payloads are Base64 encoded into a fixed PowerShell script.
- Duplicate `action_id` plus identical proposal returns its cached result. Reusing an ID for different intent/type/args returns `ACTION_ID_COLLISION`.
- `cancel(action_id)` requests cancellation. Process-backed drivers terminate their child process; a cancelled notification may already have appeared briefly, so callers must not treat cancellation as an undo operation.
- Driver errors after an external effect may have begun return `UNKNOWN`, not `FAILED`, to prevent unsafe automatic replay.
- `OBSERVE` validates `value`, `confidence` in `[0,1]`, and optional `ttl_s` in `[0.1,3600]`. The engine separately validates that the requested world key is allowlisted before admitting the observation.
- Missing real drivers fail explicitly; they are never reported as successful no-ops.

## Supported light driver

`SysfsLedDriver` only accepts a configured simple LED name and writes the Linux LED-class `brightness` node. Supported patterns are `idle`, `attention`, `success`, and `warning`. If `duration_s` is greater than zero, it restores the previous brightness at the end or on cancellation. Omit `duration_s` (or use zero) for a persistent brightness setting. Permissions and device behavior depend on the target Linux system.

The executor does not claim that a display notification was read, that speech was heard, or that a sensor inference is objectively true. It only reports what the local driver can confirm.

## Manual smoke test

Install the package and run the opt-in CLI example. It requires an explicit `--speak`, `--notify`, or `--light` argument so running it without arguments does not create side effects:

```bash
python examples/real_action_executor_demo.py --speak "Continuity Engine is running"
python examples/real_action_executor_demo.py --notify "Task complete"
python examples/real_action_executor_demo.py --light success --led-name <configured-linux-led-name>
```

The example still passes through the engine's safety path. On unsupported/headless systems, speech/notifications return a driver error. `OBSERVE` requires a host sensor provider and is not exposed by this simple CLI example.

## Important production gaps

- This adapter has not been verified on Windows/macOS/Linux target machines in this development environment; command availability and desktop-session behavior must be tested on the actual host.
- Linux `espeak` language/voice availability differs by distribution. For high-quality voice, inject a dedicated TTS driver.
- A native desktop notification is not proof the user saw/read it.
- Sysfs LED patterns are fixed brightness levels, not certified signaling or a safety indicator.
- The host software adapter is not a physical safety controller. No mobile movement action is supported.
