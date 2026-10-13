"""Application-level setup and lifecycle for a real OpenAI-compatible LLM provider."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import time
from pathlib import Path
from typing import Any

from .llm_gateway import LLMGateway, LLMRequest
from .openai_compatible import OpenAICompatibleAdapter


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    configured: bool
    reachable: bool | None
    latency_s: float | None
    model: str | None
    error_code: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"configured": self.configured, "reachable": self.reachable,
                "latency_s": self.latency_s, "model": self.model,
                "error_code": self.error_code, "note": self.note}


class LLMRuntime:
    """Owns the adapter and gateway attached to one live engine.

    Provider construction reads an explicit config path or environment variables; it
    never infers credentials or silently enables a network provider. A probe that sends
    a real inference request is opt-in because it may incur cost and transmit context.
    """
    def __init__(self, engine: Any, adapter: OpenAICompatibleAdapter, gateway: LLMGateway,
                 reliability: Any | None = None):
        self.engine, self.adapter, self.gateway = engine, adapter, gateway
        # When the reliability layer is enabled, ``adapter`` is the reliable wrapper and
        # ``delegate`` is the raw provider adapter used for transport introspection.
        self.reliability = reliability
        self.delegate = getattr(reliability, "delegate", adapter)
        self._closed = False

    @classmethod
    def attach(cls, engine: Any, *, config_file: str | os.PathLike[str] | None = None,
               env_prefix: str | None = "CONTINUITY_LLM_", timeout_s: float | None = None,
               max_concurrent: int = 2, client: Any | None = None,
               retry_policy: Any | None = None, circuit_breaker: Any | None = None,
               reliability_metrics: Any | None = None) -> "LLMRuntime":
        if bool(getattr(engine, "replay_mode", False)):
            raise RuntimeError("real network LLM providers cannot be attached in replay mode")
        if getattr(engine, "llm_gateway", None) is not None:
            raise RuntimeError("engine already has an LLM gateway; close/rebuild it explicitly before replacing providers")
        if config_file is not None and env_prefix is not None:
            raise ValueError("choose config_file or env_prefix, not both")
        if config_file is not None:
            provider = OpenAICompatibleAdapter.from_file(config_file, client=client)
        elif env_prefix is not None:
            provider = OpenAICompatibleAdapter.from_env(prefix=env_prefix, client=client)
        else:
            raise ValueError("an explicit provider config_file or env_prefix is required")
        cfg = engine.config.get() if callable(getattr(getattr(engine, "config", None), "get", None)) else {}
        llm_cfg = cfg.get("llm", {}) if isinstance(cfg, dict) else {}
        # Controlled retry / rate-limit backoff / circuit breaking is opt-in so that the
        # deterministic test suite and replay mode keep their exact previous behavior.
        reliability = None
        adapter: Any = provider
        if retry_policy is not None or circuit_breaker is not None or reliability_metrics is not None:
            from .reliability import ReliableLLMAdapter
            reliability = ReliableLLMAdapter(provider, policy=retry_policy, breaker=circuit_breaker,
                                             metrics=reliability_metrics)
            adapter = reliability
        gateway = LLMGateway(
            adapter, timeout_s=float(timeout_s if timeout_s is not None else provider.timeout_s),
            max_concurrent=max_concurrent,
            context_token_budgets=llm_cfg.get("context_token_budgets") if isinstance(llm_cfg, dict) else None,
        )
        engine.llm_gateway = gateway
        return cls(engine, adapter, gateway, reliability)

    def configuration_status(self) -> ProviderProbeResult:
        return ProviderProbeResult(True, None, None, self.adapter.model,
                                   note="Configured; remote reachability has not been tested.")

    def reliability_snapshot(self) -> dict[str, Any] | None:
        """Return retry/circuit/metric counters, or None when the layer is disabled."""
        snapshot = getattr(self.reliability, "snapshot", None)
        return snapshot() if callable(snapshot) else None

    async def reset_reliability(self) -> None:
        """Clear circuit state and counters, e.g. after a declared recovery/restart."""
        if self.reliability is None:
            return
        breaker = getattr(self.reliability, "breaker", None)
        if breaker is not None:
            breaker.reset()

    async def probe(self, *, perform_inference: bool = False, timeout_s: float = 8.0) -> ProviderProbeResult:
        """Optionally make a tiny real inference request; no prompt/user data is sent."""
        if not perform_inference:
            return self.configuration_status()
        if self._closed:
            return ProviderProbeResult(False, False, None, None, "RUNTIME_CLOSED", "Provider runtime is closed.")
        if not 0.5 <= timeout_s <= 60:
            raise ValueError("timeout_s must be in [0.5,60]")
        now = time.monotonic()
        context = {"purpose": "simple_response", "user_input": "Provider health check. Reply briefly.",
                   "relevant_memories": [], "recent_dialogue": [], "interaction_rules": {}}
        req = LLMRequest(request_id="health-" + str(int(now * 1000)), interaction_id="provider-health",
                         intent_id=None, generation=0, purpose="simple_response", created_at_mono=now,
                         deadline_mono=now + timeout_s, context_json=json.dumps(context, separators=(",", ":")),
                         output_token_budget=128)
        try:
            payload = await asyncio.wait_for(self.adapter.generate(req), timeout=timeout_s)
            if not isinstance(payload, dict) or not isinstance(payload.get("response_text"), str):
                return ProviderProbeResult(True, False, time.monotonic() - now, self.adapter.model,
                                           "PROBE_INVALID_OUTPUT", "Provider responded but output failed the dialogue shape check.")
            return ProviderProbeResult(True, True, time.monotonic() - now, self.adapter.model,
                                       note="A minimal remote inference request succeeded.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ProviderProbeResult(True, False, time.monotonic() - now, self.adapter.model,
                                       "PROBE_" + type(exc).__name__, "Provider probe failed; response text and credentials were not logged.")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.adapter.aclose()
        except Exception:
            # Application shutdown is best-effort; the provider itself never owns engine state.
            pass


__all__ = ["ProviderProbeResult", "LLMRuntime"]
