"""Optional semantic body adapter contracts. No physical device is assumed to exist."""
from __future__ import annotations
import asyncio
from typing import Any, Mapping, Protocol
from ..core.models import ActionProposal, ActionResult, ActionStatus, thaw

class BodyDriver(Protocol):
    async def execute_body_command(self, action_id: str, command: Mapping[str, Any], cancel_event: asyncio.Event) -> Mapping[str, Any]: ...
    async def health_check(self) -> bool: ...

class SemanticBodyMapper:
    """Maps bounded internal state to a small reviewed semantic command set."""
    _ALLOWED = {"startle", "alert_gaze", "slow_breath", "stretch", "idle_pose", "neutral"}
    def __init__(self, *, enabled: bool = False): self.enabled = bool(enabled)
    def map(self, *, event: str, arousal: float = 0.0, sleeping: bool = False) -> dict[str, Any] | None:
        if not self.enabled: return None
        if event not in self._ALLOWED: raise ValueError("body event is not allowlisted")
        if isinstance(arousal, bool) or not isinstance(arousal, (int, float)) or not 0 <= float(arousal) <= 1: raise ValueError("arousal must be in [0,1]")
        return {"semantic": event, "intensity": float(arousal), "sleeping": bool(sleeping)}

class BodyController:
    """Dispatches only through an explicitly injected driver and fails closed when absent."""
    def __init__(self, driver: BodyDriver | None = None, *, enabled: bool = False, mapper: SemanticBodyMapper | None = None):
        self.driver = driver; self.enabled = bool(enabled); self.mapper = mapper or SemanticBodyMapper(enabled=enabled)
        self._active_cancel: dict[str, asyncio.Event] = {}
    async def execute(self, proposal: ActionProposal, *, cancel_event: asyncio.Event | None = None) -> ActionResult:
        if proposal.action_type != "BODY": return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code="BODY_ONLY_CONTROLLER")
        if not self.enabled: return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code="BODY_DISABLED")
        args = thaw(proposal.args)
        if (set(args) != {"semantic", "intensity", "sleeping"}
                or args.get("semantic") not in SemanticBodyMapper._ALLOWED
                or isinstance(args.get("intensity"), bool)
                or not isinstance(args.get("intensity"), (int, float))
                or not 0 <= float(args.get("intensity")) <= 1
                or not isinstance(args.get("sleeping"), bool)):
            return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code="BODY_ARGS_INVALID")
        if self.driver is None: return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code="BODY_DRIVER_UNAVAILABLE")
        cancel_event = cancel_event or asyncio.Event()
        self._active_cancel[proposal.action_id] = cancel_event
        try:
            if not await self.driver.health_check(): return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code="BODY_DRIVER_UNHEALTHY")
            details = await self.driver.execute_body_command(proposal.action_id, args, cancel_event)
            if not isinstance(details, Mapping): return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code="BODY_DRIVER_INVALID_RESULT")
            return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.SUCCEEDED, details=dict(details))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.FAILED, error_code=f"BODY_DRIVER_{type(exc).__name__}")
        finally:
            self._active_cancel.pop(proposal.action_id, None)

    async def cancel(self, action_id: str) -> bool:
        event = self._active_cancel.get(action_id)
        if event is None:
            return False
        event.set()
        return True
