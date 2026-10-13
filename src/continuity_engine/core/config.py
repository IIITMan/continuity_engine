"""Configuration loading, validation and atomic allowlisted hot reload."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import math
import yaml

from ..state.autonomy import AutonomousBehaviorGenerator


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
        "policy", "llm", "memory", "personality", "affect", "fault_handling", "tasks",
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
        request_routing = config.get("request_routing", {})
        if not isinstance(request_routing, Mapping):
            raise ConfigError("request_routing must be a mapping")
        if "enabled" in request_routing and not isinstance(request_routing["enabled"], bool):
            raise ConfigError("request_routing.enabled must be boolean")
        if not isinstance(request_routing.get("default_timezone", "Asia/Tokyo"), str) or not request_routing.get("default_timezone", "Asia/Tokyo"):
            raise ConfigError("request_routing.default_timezone must be a non-empty IANA timezone name")
        rr_rounds = request_routing.get("clarification_max_rounds", interaction["clarification_max_rounds_per_intent"])
        if not isinstance(rr_rounds, int) or isinstance(rr_rounds, bool) or not 0 <= rr_rounds <= 2:
            raise ConfigError("request_routing.clarification_max_rounds must be in [0,2]")
        rr_chars = request_routing.get("max_request_chars", 10000)
        if not isinstance(rr_chars, int) or isinstance(rr_chars, bool) or not 64 <= rr_chars <= 100000:
            raise ConfigError("request_routing.max_request_chars must be in [64,100000]")
        protected = request_routing.get("protected_locations", ["卧室", "浴室", "卫生间", "bathroom"])
        if not isinstance(protected, list) or any(not isinstance(x, str) or not x.strip() for x in protected):
            raise ConfigError("request_routing.protected_locations must be a list of non-empty strings")
        capability_cfg = config.get("capabilities", {})
        if not isinstance(capability_cfg, Mapping):
            raise ConfigError("capabilities must be a mapping")
        for key in ("weather_enabled", "knowledge_search_enabled", "reminders_enabled", "timers_enabled", "media_control_enabled", "device_control_enabled", "audio_capabilities_enabled"):
            if key in capability_cfg and not isinstance(capability_cfg[key], bool):
                raise ConfigError(f"capabilities.{key} must be boolean")
        provider_services = config.get("provider_services", {})
        if not isinstance(provider_services, Mapping):
            raise ConfigError("provider_services must be a mapping")
        reminder_provider_cfg = provider_services.get("reminder", {})
        weather_provider_cfg = provider_services.get("weather", {})
        knowledge_provider_cfg = provider_services.get("knowledge", {})
        for section_name, section in (("reminder", reminder_provider_cfg), ("weather", weather_provider_cfg), ("knowledge", knowledge_provider_cfg)):
            if not isinstance(section, Mapping):
                raise ConfigError(f"provider_services.{section_name} must be a mapping")
        db_path = reminder_provider_cfg.get("database_path", "data/reminders.sqlite3")
        if not isinstance(db_path, str) or not db_path.strip():
            raise ConfigError("provider_services.reminder.database_path must be a non-empty string")
        for key, low, high in (("poll_interval_s", 0.05, 60.0), ("delivery_timeout_s", 0.5, 120.0), ("max_horizon_s", 1.0, 31622400.0)):
            value = _finite_float(reminder_provider_cfg.get(key, {"poll_interval_s": 0.5, "delivery_timeout_s": 15.0, "max_horizon_s": 2592000}[key]), f"provider_services.reminder.{key}")
            if not low <= value <= high:
                raise ConfigError(f"provider_services.reminder.{key} must be in [{low},{high}]")
        for key, low, high in (("timeout_s", 1.0, 30.0),):
            value = _finite_float(weather_provider_cfg.get(key, 8.0), f"provider_services.weather.{key}")
            if not low <= value <= high:
                raise ConfigError(f"provider_services.weather.{key} must be in [{low},{high}]")
            value = _finite_float(knowledge_provider_cfg.get(key, 10.0), f"provider_services.knowledge.{key}")
            if not low <= value <= high:
                raise ConfigError(f"provider_services.knowledge.{key} must be in [{low},{high}]")
        max_results = knowledge_provider_cfg.get("max_results", 3)
        max_extract_chars = knowledge_provider_cfg.get("max_extract_chars", 1400)
        cache_ttl = _finite_float(knowledge_provider_cfg.get("cache_ttl_s", 900.0), "provider_services.knowledge.cache_ttl_s")
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 5:
            raise ConfigError("provider_services.knowledge.max_results must be an integer in [1,5]")
        if not isinstance(max_extract_chars, int) or isinstance(max_extract_chars, bool) or not 200 <= max_extract_chars <= 5000:
            raise ConfigError("provider_services.knowledge.max_extract_chars must be an integer in [200,5000]")
        if not 0 <= cache_ttl <= 86400:
            raise ConfigError("provider_services.knowledge.cache_ttl_s must be in [0,86400]")
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
        tasks = config["tasks"]
        if not isinstance(tasks, Mapping):
            raise ConfigError("tasks must be a mapping")
        if not isinstance(tasks.get("max_steps"), int) or isinstance(tasks.get("max_steps"), bool) or not 1 <= tasks["max_steps"] <= 32:
            raise ConfigError("tasks.max_steps must be an integer in [1,32]")
        plan_attempts = tasks.get("plan_generation_max_attempts", 2)
        if not isinstance(plan_attempts, int) or isinstance(plan_attempts, bool) or not 1 <= plan_attempts <= 3:
            raise ConfigError("tasks.plan_generation_max_attempts must be an integer in [1,3]")
        observe_keys = tasks.get("observe_world_key_allowlist", [])
        if (not isinstance(observe_keys, list)
                or any(not isinstance(key, str) or not key or len(key) > 128 for key in observe_keys)
                or len(set(observe_keys)) != len(observe_keys)):
            raise ConfigError("tasks.observe_world_key_allowlist must be a unique list of non-empty keys")
        llm_world_keys = set(llm.get("world_fact_allowlist", []))
        if not set(observe_keys) <= llm_world_keys:
            raise ConfigError("tasks.observe_world_key_allowlist must be a subset of llm.world_fact_allowlist")
        if not isinstance(tasks.get("max_plan_json_bytes"), int) or tasks["max_plan_json_bytes"] < 1024:
            raise ConfigError("tasks.max_plan_json_bytes must be >= 1024")
        if not isinstance(tasks.get("history_limit"), int) or tasks["history_limit"] < 1:
            raise ConfigError("tasks.history_limit must be >= 1")
        for name, low, high in (("default_priority", 0.0, 1.0), ("default_step_timeout_s", 0.1, 600.0),
                                ("maximum_step_timeout_s", 0.1, 600.0), ("maximum_task_deadline_s", 0.1, 86400.0)):
            value = _finite_float(tasks[name], f"tasks.{name}")
            if not low <= value <= high:
                raise ConfigError(f"tasks.{name} must be in [{low},{high}]")
        if tasks["default_step_timeout_s"] > tasks["maximum_step_timeout_s"]:
            raise ConfigError("tasks.default_step_timeout_s cannot exceed maximum_step_timeout_s")
        if not isinstance(tasks.get("enabled", True), bool):
            raise ConfigError("tasks.enabled must be boolean")
        if float(tasks["default_priority"]) < float(policy["ordinary_candidate_threshold"]):
            raise ConfigError("tasks.default_priority must be >= policy.ordinary_candidate_threshold")
        action_allowlist = tasks.get("llm_plannable_action_types", [])
        supported_task_actions = {"SPEAK", "NOTIFY", "OBSERVE", "WAIT", "LIGHT"}
        if not isinstance(action_allowlist, list) or any(not isinstance(x, str) or x not in supported_task_actions for x in action_allowlist):
            raise ConfigError("tasks.llm_plannable_action_types must be a list of supported action types")
        if tasks.get("parallel_steps_enabled") is not False or tasks.get("conditional_steps_enabled") is not False:
            raise ConfigError("current task-plan contract supports sequential linear plans only")
        action_execution = config.get("action_execution", {})
        if not isinstance(action_execution, Mapping):
            raise ConfigError("action_execution must be a mapping")
        if action_execution.get("mode", "simulated") not in {"simulated", "real"}:
            raise ConfigError("action_execution.mode must be simulated or real")
        max_text_chars = action_execution.get("max_text_chars", 2000)
        cache_size = action_execution.get("result_cache_size", 10000)
        if not isinstance(max_text_chars, int) or isinstance(max_text_chars, bool) or not 1 <= max_text_chars <= 10000:
            raise ConfigError("action_execution.max_text_chars must be an integer in [1,10000]")
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 100:
            raise ConfigError("action_execution.result_cache_size must be an integer >= 100")
        if not 0 <= _finite_float(action_execution.get("max_wait_s", 300.0), "action_execution.max_wait_s") <= 3600:
            raise ConfigError("action_execution.max_wait_s must be in [0,3600]")
        for section_name in ("speech", "notification", "sysfs_led"):
            if not isinstance(action_execution.get(section_name, {}), Mapping):
                raise ConfigError(f"action_execution.{section_name} must be a mapping")
        speech_cfg = action_execution.get("speech", {})
        if not isinstance(speech_cfg.get("enabled", True), bool):
            raise ConfigError("action_execution.speech.enabled must be boolean")
        if speech_cfg.get("backend", "platform_native") != "platform_native":
            raise ConfigError("action_execution.speech.backend must be platform_native")
        rate = speech_cfg.get("rate_wpm", 180)
        if not isinstance(rate, int) or isinstance(rate, bool) or not 80 <= rate <= 350:
            raise ConfigError("action_execution.speech.rate_wpm must be an integer in [80,350]")
        notification_cfg = action_execution.get("notification", {})
        if not isinstance(notification_cfg.get("enabled", True), bool):
            raise ConfigError("action_execution.notification.enabled must be boolean")
        if notification_cfg.get("backend", "platform_native") != "platform_native":
            raise ConfigError("action_execution.notification.backend must be platform_native")
        display_seconds = notification_cfg.get("display_seconds", 4)
        if not isinstance(display_seconds, int) or isinstance(display_seconds, bool) or not 1 <= display_seconds <= 10:
            raise ConfigError("action_execution.notification.display_seconds must be an integer in [1,10]")
        led_cfg = action_execution.get("sysfs_led", {})
        if not isinstance(led_cfg.get("enabled", False), bool):
            raise ConfigError("action_execution.sysfs_led.enabled must be boolean")
        if led_cfg.get("enabled") and (not isinstance(led_cfg.get("led_name"), str) or not led_cfg.get("led_name")):
            raise ConfigError("action_execution.sysfs_led.led_name is required when enabled")
        try:
            AutonomousBehaviorGenerator.from_mapping(config)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid autonomy configuration: {exc}") from exc
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
