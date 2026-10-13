"""Opt-in real host action smoke test.

This example has no side effects unless the caller explicitly chooses --speak,
--notify or --light. It dispatches through ContinuityEngine so local safety policy
is applied before the actual executor runs.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from continuity_engine import ConfigManager, ContinuityEngine, EventKind, action_executor_from_config


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--speak", metavar="TEXT", help="speak text using the host OS TTS")
    choice.add_argument("--notify", metavar="TEXT", help="show a host desktop notification")
    choice.add_argument("--light", choices=("idle", "attention", "success", "warning"), help="set a configured Linux sysfs LED pattern")
    parser.add_argument("--title", default="Continuity Engine", help="notification title")
    parser.add_argument("--led-name", help="simple LED name under /sys/class/leds (required with --light)")
    parser.add_argument("--led-root", default="/sys/class/leds", help="sysfs LED-class root")
    args = parser.parse_args()

    if args.light and not args.led_name:
        parser.error("--light requires --led-name")

    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    settings = config.get()
    settings["action_execution"]["mode"] = "real"
    settings["action_execution"]["speech"]["enabled"] = bool(args.speak)
    settings["action_execution"]["notification"]["enabled"] = bool(args.notify)
    settings["action_execution"]["sysfs_led"]["enabled"] = bool(args.light)
    if args.light:
        settings["action_execution"]["sysfs_led"]["led_name"] = args.led_name
        settings["action_execution"]["sysfs_led"]["root"] = args.led_root
    executor = action_executor_from_config(settings)
    engine = ContinuityEngine(config, action_executor=executor)
    try:
        if args.speak is not None:
            action_type = "SPEAK"
            action_args = {"text": args.speak, "mode": "local_template"}
            goal = "operator requested speech"
        elif args.notify is not None:
            action_type = "NOTIFY"
            action_args = {"text": args.notify, "title": args.title}
            goal = "operator requested desktop notification"
        else:
            action_type = "LIGHT"
            action_args = {"pattern": args.light}
            goal = "operator requested LED pattern"

        event = engine.new_event(
            "INTENT_CANDIDATE",
            {
                "intent_type": "operator_action",
                "goal": goal,
                "score": 0.99,
                "urgency": 0.5,
                "reason": "explicit_operator_requested_action",
                "risk_level": "low",
                "action_type": action_type,
                "action_args": action_args,
                "ordinary_proactive": False,
            },
            source="real_action_executor_demo",
        )
        engine.inject(event)
        step = engine.step(dt=0.0)
        if not step.proposals:
            print("No action proposal was produced:", step.diagnostics)
            return 2
        proposal = step.proposals[0]
        decision, result = await engine.dispatch_action(proposal)
        print("Safety decision:", decision.outcome, decision.code)
        print("Action result:", result)
        engine.step(dt=0.0)  # Consume the terminal receipt, if one was enqueued.
        return 0 if result is not None and result.status.value == "SUCCEEDED" else 1
    finally:
        await engine.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
