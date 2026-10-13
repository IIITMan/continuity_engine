"""Action executor contracts and safe simulated adapter for tests/replay."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol

from ..core.models import ActionProposal, ActionResult, ActionStatus


class ActionExecutor(Protocol):
    is_simulated: bool

    async def execute(self, proposal: ActionProposal) -> ActionResult: ...
    async def cancel(self, action_id: str) -> bool: ...
    async def health_check(self) -> bool: ...


class SimulatedActionExecutor:
    """Deterministic no-hardware executor. Safe default for the MVP and all replay runs."""
    is_simulated = True

    def __init__(self, result_by_action_type: dict[str, ActionStatus] | None = None, latency_s: float = 0.0,
                 details_by_action_type: dict[str, dict] | None = None):
        if latency_s < 0:
            raise ValueError("latency_s must be >= 0")
        self.result_by_action_type = dict(result_by_action_type or {})
        self.details_by_action_type = dict(details_by_action_type or {})
        self.latency_s = latency_s
        self.started: list[ActionProposal] = []
        self.results: dict[str, ActionResult] = {}
        self.cancelled: set[str] = set()
        self.calls_by_id: dict[str, int] = defaultdict(int)
        self.healthy = True

    async def execute(self, proposal: ActionProposal) -> ActionResult:
        self.calls_by_id[proposal.action_id] += 1
        if proposal.action_id in self.results:
            return self.results[proposal.action_id]
        self.started.append(proposal)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if proposal.action_id in self.cancelled:
            result = ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.CANCELLED, error_code="CANCELLED_BEFORE_COMPLETE")
        else:
            status = self.result_by_action_type.get(proposal.action_type, ActionStatus.SUCCEEDED)
            result = ActionResult(proposal.action_id, proposal.intent_id, status,
                                  error_code=None if status == ActionStatus.SUCCEEDED else status.value,
                                  details=self.details_by_action_type.get(proposal.action_type, {}))
        self.results[proposal.action_id] = result
        return result

    async def cancel(self, action_id: str) -> bool:
        # Terminal results are immutable; cancellation only affects work not yet completed.
        if action_id in self.results:
            return False
        self.cancelled.add(action_id)
        return True

    async def health_check(self) -> bool:
        return self.healthy


class ReplayActionExecutor(SimulatedActionExecutor):
    """Named type for strict replay dependency checks."""
    is_replay_adapter = True
