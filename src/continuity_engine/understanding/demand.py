"""Structured demand understanding, intentionally separate from engine routing."""
from __future__ import annotations

import uuid
from typing import Any, Mapping

from ..llm.llm_gateway import LLMGateway, LLMStatus
from ..llm.state_views import StateView


class LLMDemandInterpreter:
    """Produce a validated semantic interpretation; never chooses or executes a route."""

    def __init__(self, gateway: LLMGateway, *, timeout_s: float | None = None):
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.gateway = gateway
        self.timeout_s = timeout_s

    async def understand(
        self,
        text: str,
        *,
        request_context: Mapping[str, Any] | None = None,
        personality_snapshot: Mapping[str, Any] | None = None,
        affect_guidance: str = "",
        interaction_state_snapshot: Mapping[str, Any] | None = None,
        voice_signals: Mapping[str, Any] | None = None,
        interaction_id: str | None = None,
        generation: int = 1,
    ) -> Mapping[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError("text must contain 1..12000 characters")
        # These are host-provided projections. Keep them bounded and JSON-compatible;
        # the gateway independently validates the full output contract.
        view = StateView(
            purpose="demand_understanding", schema_version="1.0",
            _fields={
                "purpose": "demand_understanding",
                "user_input": text,
                "request_context": dict(request_context or {}),
                "personality_snapshot": dict(personality_snapshot or {}),
                "affect_guidance": affect_guidance[:1200] if isinstance(affect_guidance, str) else "",
                "interaction_state_snapshot": dict(interaction_state_snapshot or {}),
                "voice_signals": dict(voice_signals or {}),
            },
        )
        result = await self.gateway.generate(
            interaction_id=interaction_id or "demand-" + uuid.uuid4().hex,
            intent_id=None, generation=generation, purpose="demand_understanding",
            view=view, timeout_s=self.timeout_s,
        )
        if result.status != LLMStatus.COMPLETED:
            raise RuntimeError("demand_understanding_" + (result.error_code or result.status.value).lower())
        return dict(result.payload)
