"""Configuration loading, validation and atomic allowlisted hot reload."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import math
import yaml


class ConfigError(ValueError):
    pass


def _flatten(obj: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            result.update(_flatten(value, path))
        else:
            result[path] = value
    return result


def _is_path_allowed(path: str, allowlist: list[str]) -> bool:
    return any(path == allowed or path.startswith(allowed + ".") for allowed in allowlist)


def _finite_float(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{name} must be finite")
    return number


def validate_config(config: Mapping[str, Any]) -> None:
    required_sections = {
        "runtime", "product", "interaction", "world_model", "drives", "prediction",
        "policy", "llm", "memory", "personality", "affect", "fault_handling",
        "observability", "performance_targets",
    }
    missing = required_sections - set(config)
    if missing:
        raise ConfigError(f"missing config sections: {sorted(missing)}")
    try:
        runtime = config["runtime"]
        interaction = config["interaction"]
        policy = config["policy"]
        llm = config["llm"]
        drives = config["drives"]
        if _finite_float(runtime["decision_period_s"], "runtime.decision_period_s") <= 0:
            raise ConfigError("runtime.decision_period_s must be > 0")
        if int(runtime["max_queue_size"]) < 1 or int(runtime["event_batch_max"]) < 1:
            raise ConfigError("event queue and batch limits must be positive")
        if int(interaction["clarification_max_rounds_per_intent"]) != 2:
            raise ConfigError("v0.2 contract fixes clarification rounds at 2")
        proactive = interaction["ordinary_proactive_messages"]
        rolling_window = _finite_float(proactive["rolling_window_s"], "interaction.ordinary_proactive_messages.rolling_window_s")
        cooldown = _finite_float(proactive["cooldown_s"], "interaction.ordinary_proactive_messages.cooldown_s")
        if int(proactive["max_count"]) < 0 or rolling_window <= 0 or cooldown < 0:
            raise ConfigError("invalid proactive interaction limits")
        switch_margin = _finite_float(policy["switch_margin"], "policy.switch_margin")
        min_dwell = _finite_float(policy["min_dwell_time_s"], "policy.min_dwell_time_s")
        if not 0 <= switch_margin <= 1:
            raise ConfigError("policy.switch_margin must be in [0,1]")
        if min_dwell < 0:
            raise ConfigError("policy.min_dwell_time_s must be >= 0")
        if int(policy["consecutive_evaluations_to_switch"]) < 1:
            raise ConfigError("policy.consecutive_evaluations_to_switch must be >= 1")
        if _finite_float(llm["default_timeout_s"], "llm.default_timeout_s") <= 0 or int(llm["max_concurrent_requests"]) < 1:
            raise ConfigError("invalid LLM timeout/concurrency")
        if not isinstance(llm.get("allowed_tools", []), list) or any(not isinstance(x, str) or not x for x in llm.get("allowed_tools", [])):
            raise ConfigError("llm.allowed_tools must be a list of non-empty strings")
        if not isinstance(llm.get("world_fact_allowlist", []), list) or any(not isinstance(x, str) or not x for x in llm.get("world_fact_allowlist", [])):
            raise ConfigError("llm.world_fact_allowlist must be a list of non-empty strings")
        budgets = llm.get("context_token_budgets", {})
        if not isinstance(budgets, Mapping) or any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in budgets.values()):
            raise ConfigError("llm.context_token_budgets must contain positive integer budgets")
        world = config["world_model"]
        confirm = _finite_float(world.get("confirm_confidence", 0.90), "world_model.confirm_confidence")
        probable = _finite_float(world.get("probable_confidence", 0.55), "world_model.probable_confidence")
        conflict = _finite_float(world.get("conflict_confidence", 0.60), "world_model.conflict_confidence")
        if any(value < 0 or value > 1 for value in (confirm, probable, conflict)) or probable > confirm:
            raise ConfigError("world-model confidence thresholds must be in [0,1] and probable <= confirm")
        for key in ("presence_observation_ttl_s", "identity_observation_ttl_s", "transient_sound_ttl_s"):
            if _finite_float(world[key], f"world_model.{key}") <= 0:
                raise ConfigError(f"world_model.{key} must be > 0")
        for name, entry in drives["default"].items():
            tau = entry.get("tau_s")
            if tau is not None and _finite_float(tau, f"drives.default.{name}.tau_s") <= 0:
                raise ConfigError(f"drives.default.{name}.tau_s must be > 0 or null")
        reserved = int(runtime["queue_overload"]["reserved_control_capacity"])
        if reserved < 1:
            raise ConfigError("reserved control capacity must be positive")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"malformed configuration: {exc}") from exc


@dataclass(frozen=True)
class ConfigSnapshot:
    version: int
    data: Mapping[str, Any]


class ConfigManager:
    """Stages a full validated config and swaps atomically at a decision boundary."""

    def __init__(self, data: Mapping[str, Any]):
        validate_config(data)
        self._data = deepcopy(dict(data))
        self._version = 1
        self._pending: dict[str, Any] | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConfigManager":
        with Path(path).open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")
        return cls(data)

    @property
    def version(self) -> int:
        return self._version

    def get(self) -> dict[str, Any]:
        return deepcopy(self._data)

    def snapshot(self) -> ConfigSnapshot:
        return ConfigSnapshot(self._version, deepcopy(self._data))

    def stage_reload(self, candidate: Mapping[str, Any]) -> int:
        """Validate the whole candidate; reject any non-allowlisted leaf change."""
        candidate_copy = deepcopy(dict(candidate))
        validate_config(candidate_copy)
        reload_cfg = self._data["runtime"]["config_reload"]
        if not reload_cfg.get("enabled", False):
            raise ConfigError("hot reload is disabled")
        old_flat = _flatten(self._data)
        new_flat = _flatten(candidate_copy)
        changed = {p for p in set(old_flat) | set(new_flat) if old_flat.get(p, object()) != new_flat.get(p, object())}
        allowlist = list(reload_cfg.get("reloadable_paths", []))
        blocked = sorted(p for p in changed if not _is_path_allowed(p, allowlist))
        if blocked:
            raise ConfigError("reload contains non-allowlisted changes: " + ", ".join(blocked))
        # Keep the currently frozen reload policy, not a changed policy from candidate.
        if _flatten(candidate_copy)["runtime.config_reload.enabled"] != _flatten(self._data)["runtime.config_reload.enabled"]:
            raise ConfigError("config reload policy cannot be hot-reloaded")
        self._pending = candidate_copy
        return self._version + 1

    def apply_pending(self) -> int:
        if self._pending is not None:
            validate_config(self._pending)
            self._data = self._pending
            self._pending = None
            self._version += 1
        return self._version

    def discard_pending(self) -> None:
        self._pending = None
