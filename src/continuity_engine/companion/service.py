"""End-to-end text companion loop built on the existing deterministic engine.

The service owns only bounded, volatile per-session transcript context. The engine
continues to own intent policy, safety, persistence and the independent control lane.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
from typing import Any, Mapping

from ..core.models import EventKind, Priority
from ..execution.expression import prepare_speech_segments
from ..llm.llm_gateway import LLMStatus
from .emotion import EmotionContinuity
from .memory import MemoryLifecycle
from .retrieval import HybridMemoryRetriever
from .observability import CompanionMetrics
from .context_budget import DialogueContextBudgeter, estimate_tokens


class CompanionStatus(str, Enum):
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    SILENCED = "SILENCED"
    CONTROL_ACCEPTED = "CONTROL_ACCEPTED"
    STALE = "STALE"
    FAILED = "FAILED"
    EMPTY_INPUT = "EMPTY_INPUT"
    MEMORY_SAVED = "MEMORY_SAVED"


@dataclass(frozen=True, slots=True)
class CompanionResult:
    status: CompanionStatus
    interaction_id: str
    generation: int | None
    response_text: str = ""
    degraded: bool = False
    error_code: str | None = None
    memory_id: str | None = None
    memory_candidate_id: str | None = None
    reason_code: str = "dialogue_completed"
    speech_segments: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def speak_allowed(self) -> bool:
        return self.status in {CompanionStatus.COMPLETED, CompanionStatus.DEGRADED} and bool(self.response_text.strip())


class CompanionService:
    """Conversation façade for v0.17+.

    Normal text chat uses one model request by default. More context changes the request
    purpose to `complex_dialogue`, but does not require a separate intent-understanding
    call. Control phrases are handled on the engine's independent event/control lane.
    """
    def __init__(self, engine: Any, *, max_history_messages: int = 32,
                 max_output_chars: int = 4000, clock=None, session_store: Any | None = None,
                 metrics: CompanionMetrics | None = None,
                 memory_retriever: HybridMemoryRetriever | None = None,
                 context_budgeter: DialogueContextBudgeter | None = None):
        if not 4 <= max_history_messages <= 200:
            raise ValueError("max_history_messages must be in [4,200]")
        if not 80 <= max_output_chars <= 8000:
            raise ValueError("max_output_chars must be in [80,8000]")
        self.engine = engine
        self.max_history_messages = max_history_messages
        self.max_output_chars = max_output_chars
        self.clock = clock or getattr(getattr(engine, "clock", None), "wall_time", time.time)
        self.emotion = EmotionContinuity(clock=self.clock)  # default-session compatibility alias
        self._emotions: dict[str, EmotionContinuity] = {}
        self.memory = MemoryLifecycle(engine, clock=self.clock, retriever=memory_retriever)
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._loaded_sessions: set[str] = set()
        self.session_store = session_store
        self.metrics = metrics or CompanionMetrics()
        self.context_budgeter = context_budgeter or DialogueContextBudgeter()
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def _session_lock(self, interaction_id: str) -> asyncio.Lock:
        async with self._lock:
            return self._locks.setdefault(interaction_id, asyncio.Lock())

    def _blocked_reason(self) -> str | None:
        state = self.engine.state
        latches = self.engine.router.peek_latches()
        if state.safety_stopped or latches.safety_stop:
            return "safety_stop"
        effective_muted = latches.muted if latches.muted is not None else state.output_muted
        if effective_muted:
            return "output_muted"
        effective_quiet = latches.quiet_scope if latches.quiet_changed else state.quiet_scope
        if effective_quiet is not None:
            return "quiet_mode"
        # The router latch is the latest ingress-time level; EngineState may lag until
        # the next deterministic tick (or contain a pre-restart value).
        if latches.user_speaking:
            return "user_speaking"
        if latches.user_stop:
            return "user_stop"
        return None

    @staticmethod
    def _control_for_text(text: str) -> tuple[str, dict[str, Any]] | None:
        normalized = " ".join(text.strip().casefold().strip("。.!！?？ ").split())
        mapping: dict[str, tuple[str, dict[str, Any]]] = {
            "停止": (EventKind.USER_STOP.value, {}), "停下": (EventKind.USER_STOP.value, {}),
            "停一下": (EventKind.USER_STOP.value, {}), "别说了": (EventKind.USER_STOP.value, {}),
            "取消当前操作": (EventKind.USER_CANCEL.value, {}), "取消": (EventKind.USER_CANCEL.value, {}),
            "静音": (EventKind.USER_MUTE.value, {}), "关闭声音": (EventKind.USER_MUTE.value, {}),
            "取消静音": (EventKind.USER_UNMUTE.value, {}), "恢复声音": (EventKind.USER_UNMUTE.value, {}),
            "进入安静模式": (EventKind.USER_QUIET.value, {"enabled": True}),
            "开启安静模式": (EventKind.USER_QUIET.value, {"enabled": True}),
            "退出安静模式": (EventKind.USER_QUIET.value, {"enabled": False}),
            "关闭安静模式": (EventKind.USER_QUIET.value, {"enabled": False}),
        }
        return mapping.get(normalized)

    async def chat(self, user_input: str, *, interaction_id: str = "default",
                   emotion_assessment: Mapping[str, Any] | None = None,
                   voice_signals: Mapping[str, Any] | None = None,
                   input_channel: str = "text") -> CompanionResult:
        """Instrumented public entry point; metrics never record raw user text."""
        started = time.monotonic()
        try:
            result = await self._chat_impl(user_input, interaction_id=interaction_id,
                                           emotion_assessment=emotion_assessment,
                                           voice_signals=voice_signals, input_channel=input_channel)
        except asyncio.CancelledError:
            self.metrics.record(status="CANCELLED", latency_s=max(0.0, time.monotonic()-started),
                                channel=input_channel, error_code="CANCELLED")
            raise
        except Exception as exc:
            self.metrics.record(status="EXCEPTION", latency_s=max(0.0, time.monotonic()-started),
                                channel=input_channel, error_code=type(exc).__name__)
            raise
        self.metrics.record(status=result.status.value, latency_s=max(0.0, time.monotonic()-started),
                            channel=input_channel, error_code=result.error_code,
                            context_tokens=result.metadata.get("variable_context_estimated_tokens"))
        return result

    async def _chat_impl(self, user_input: str, *, interaction_id: str = "default",
                   emotion_assessment: Mapping[str, Any] | None = None,
                   voice_signals: Mapping[str, Any] | None = None,
                   input_channel: str = "text") -> CompanionResult:
        if not isinstance(user_input, str) or len(user_input) > 12000:
            raise ValueError("user_input must be a string up to 12000 characters")
        if not interaction_id or len(interaction_id) > 200:
            raise ValueError("interaction_id must contain 1..200 characters")
        if input_channel not in {"text", "voice"}:
            raise ValueError("input_channel must be 'text' or 'voice'")
        text = user_input.strip()
        if not text:
            return CompanionResult(CompanionStatus.EMPTY_INPUT, interaction_id, None,
                                   error_code="EMPTY_INPUT", reason_code="empty_input")

        control = self._control_for_text(text)
        if control is not None:
            kind, payload = control
            # Cancellation-like controls invalidate in-flight results synchronously;
            # consuming the event latch later must not revive an old answer.
            if kind in {EventKind.USER_STOP.value, EventKind.USER_CANCEL.value, EventKind.USER_MUTE.value} or (
                    kind == EventKind.USER_QUIET.value and payload.get("enabled", True)):
                self.engine.invalidate_interactions(reason_code=f"control:{kind}",
                                                    interaction_id=interaction_id if kind == EventKind.USER_CANCEL.value else None)
            event = self.engine.new_event(kind, payload, source="companion_control", priority=int(Priority.CRITICAL))
            accepted = self.engine.inject(event)
            return CompanionResult(CompanionStatus.CONTROL_ACCEPTED if accepted else CompanionStatus.FAILED,
                                   interaction_id, self.engine.current_generation(interaction_id),
                                   error_code=None if accepted else "CONTROL_QUEUE_REJECTED",
                                   reason_code="direct_control_lane", metadata={"control_kind": kind})

        # Detect credential-like content in an explicit memory request before it can
        # enter session history, the event queue, or an external model context.
        import re
        memory_match = re.match(r"^\s*(?:请)?(?:帮我)?(?:记住|记一下|记下来)[：:，,、\s]*(.{2,1800})\s*$", text)
        sensitive_memory_request = bool(memory_match and self.memory._sensitive(memory_match.group(1)))
        safe_event_text = "[sensitive memory request redacted]" if sensitive_memory_request else text
        # A special event flag prevents duplicate memory writes and raw text snapshotting
        # when the core tick later processes this service-managed interaction.
        if input_channel == "text":
            # Text input can barge in on host-managed speech currently playing in the
            # same session; voice input handles this through its own task/generation path.
            self.engine.interrupt_outputs(reason_code="new_text_input", interaction_id=interaction_id)
        source_event = self.engine.new_event(
            EventKind.USER_MESSAGE.value,
            {"text": safe_event_text, "interaction_id": interaction_id, "suppress_auto_memory": True,
             "dialogue_service_managed": True,
             "source_channel": "voice" if voice_signals else "text"},
            source="companion_service", priority=int(Priority.HIGH),
        )
        explicit_memory_id = None
        try:
            explicit_memory_id = None if sensitive_memory_request else self.memory.explicit_request(text, source_event.event_id)
        except (RuntimeError, ValueError, TypeError) as exc:
            # Fail closed on the memory write, but allow normal dialogue to explain the
            # storage limitation instead of claiming the memory was saved.
            explicit_memory_error = type(exc).__name__
        else:
            explicit_memory_error = None
        accepted = self.engine.inject(source_event)
        if not accepted:
            return CompanionResult(CompanionStatus.FAILED, interaction_id,
                                   self.engine.current_generation(interaction_id),
                                   error_code="USER_MESSAGE_QUEUE_REJECTED", reason_code="input_not_accepted")
        generation = self.engine.current_generation(interaction_id)

        candidate = None
        if explicit_memory_id is None:
            candidate = self.memory.propose_from_utterance(text, source_id=source_event.event_id)

        # User affect is a conservative estimate, held in volatile memory only. Voice
        # labels are optional model outputs and are accepted only at a high confidence.
        emotion = self._emotion_for(interaction_id)
        observed = emotion.observe("[sensitive content omitted]" if sensitive_memory_request else text,
                                   model_assessment=None if sensitive_memory_request else emotion_assessment,
                                   source="voice_model" if emotion_assessment else "text")
        blocked = self._blocked_reason()
        if blocked is not None:
            return CompanionResult(CompanionStatus.SILENCED, interaction_id, generation,
                                   memory_id=explicit_memory_id,
                                   memory_candidate_id=None if candidate is None else candidate.candidate_id,
                                   error_code=blocked.upper(), reason_code=blocked,
                                   metadata={"emotion_estimate": observed.to_dict()})

        lock = await self._session_lock(interaction_id)
        async with lock:
            if interaction_id not in self._loaded_sessions:
                loaded: list[dict[str, Any]] = []
                store = self.session_store
                loader = getattr(store, "load_session", None) if store is not None else None
                if callable(loader):
                    try:
                        loaded = loader(interaction_id, limit=self.max_history_messages)
                    except Exception:
                        # Persistence failure is visible in diagnostics/metrics but cannot
                        # block a live chat or manufacture restored context.
                        loaded = []
                clean_loaded = []
                for row in loaded if isinstance(loaded, list) else []:
                    if (isinstance(row, Mapping) and row.get("role") in {"user", "assistant"}
                            and isinstance(row.get("content"), str) and len(row["content"]) <= self.max_output_chars * 3):
                        try:
                            row_generation = max(0, int(row.get("generation", 0)))
                            row_timestamp = float(row.get("timestamp", self.clock()))
                            if not (row_timestamp == row_timestamp and abs(row_timestamp) != float("inf")):
                                continue
                        except (ValueError, TypeError, OverflowError):
                            continue
                        clean_loaded.append({"role": row["role"], "content": row["content"],
                                             "generation": row_generation, "timestamp": row_timestamp})
                clean_loaded = clean_loaded[-self.max_history_messages:]
                # Rebase old generations into a negative volatile range. Engine
                # generations may restart from zero after process restart; persisted
                # values must never sort a new turn before yesterday's context.
                base = -len(clean_loaded)
                for index, row in enumerate(clean_loaded):
                    row["generation"] = base + index
                self._history[interaction_id] = clean_loaded
                self._loaded_sessions.add(interaction_id)
            history = self._history.setdefault(interaction_id, [])
            history.append({"role": "user", "content": "[sensitive content omitted]" if sensitive_memory_request else text,
                            "generation": generation, "timestamp": float(self.clock())})
            history.sort(key=lambda x: (int(x["generation"]), 0 if x["role"] == "user" else 1))
            history[:] = history[-self.max_history_messages:]
            self._persist_turn(interaction_id, history[-1])
            # The current utterance is already provided as `user_input` in the LLM view.
            # Excluding it here prevents paying for it twice on every multi-turn request.
            prior_history = [x for x in history if not (x.get("generation") == generation and x.get("role") == "user")]
            recent_dialogue = [{"role": x["role"], "text": x["content"]} for x in prior_history[-12:]]
        # Explicit preference requests are handled locally so the response can accurately
        # report the actual repository outcome.
        if explicit_memory_id is not None:
            response = "记住了，我已经把这条内容保存为长期记忆。"
            if not self._generation_current(interaction_id, generation):
                return CompanionResult(CompanionStatus.STALE, interaction_id, generation, error_code="GENERATION_STALE")
            self._append_assistant(interaction_id, generation, response)
            return CompanionResult(CompanionStatus.MEMORY_SAVED, interaction_id, generation, response,
                                   memory_id=explicit_memory_id, reason_code="explicit_memory_saved",
                                   speech_segments=(response,), metadata={"emotion_estimate": observed.to_dict()})
        if sensitive_memory_request:
            # Never forward, echo, or persist credential-like text as a memory candidate.
            response = "这条内容可能包含敏感凭据，我不会保存它，也不会把它发送给语言模型。"
            self._append_assistant(interaction_id, generation, response)
            return CompanionResult(CompanionStatus.COMPLETED, interaction_id, generation, response,
                                   reason_code="sensitive_memory_rejected", speech_segments=(response,))
        is_explicit_memory_request = memory_match is not None
        if explicit_memory_error is not None and is_explicit_memory_request:
            response = "我还没有连接可用的长期记忆存储，所以这次没有保存。你可以先把内容保存在本地，接入记忆库后再让我记录。"
            self._append_assistant(interaction_id, generation, response)
            return CompanionResult(CompanionStatus.FAILED, interaction_id, generation, response,
                                   error_code="MEMORY_STORE_UNAVAILABLE", reason_code="memory_not_saved",
                                   speech_segments=(response,))

        # Over-retrieve so the budgeter can keep the strongest recent context instead of
        # dropping whole categories. The budgeter also performs the projection down to the
        # reviewed LLM-bound field set (the retriever returns ranking metadata such as
        # retrieval_score that the strict state-view allowlist rejects).
        raw_memories = self.memory.recall(text, limit=12)
        budget = self.context_budgeter.prepare(recent_dialogue, raw_memories)
        recent_dialogue = list(budget.dialogue)
        memories = list(budget.memories)
        purpose = "complex_dialogue" if recent_dialogue else "simple_response"
        context_tokens = budget.variable_context_tokens + estimate_tokens(text)
        kwargs: dict[str, Any] = {
            "purpose": purpose,
            "interaction_id": interaction_id,
            "generation": generation,
            "user_input": text,
            "relevant_memories": memories,
            "affect_guidance": self.engine.affect_personality.expression_guidance() + "\n" + emotion.guidance(),
        }
        if purpose == "complex_dialogue":
            kwargs["recent_dialogue"] = recent_dialogue
        if voice_signals and isinstance(voice_signals, Mapping):
            # Never pass raw audio or arbitrary adapter metadata into a language-model view.
            kind = voice_signals.get("kind")
            value = voice_signals.get("value")
            if isinstance(kind, str) and isinstance(value, (str, float, int, bool)):
                kwargs["affect_guidance"] += f"\n经过白名单处理的语音线索：{kind}={str(value)[:80]}。该线索不等于事实。"
        try:
            result = await self.engine.request_llm(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return CompanionResult(CompanionStatus.FAILED, interaction_id, generation,
                                   error_code=f"DIALOGUE_{type(exc).__name__}", reason_code="dialogue_exception")

        if not self._generation_current(interaction_id, generation):
            return CompanionResult(CompanionStatus.STALE, interaction_id, generation,
                                   error_code="GENERATION_STALE", reason_code="newer_input_superseded")
        blocked_after = self._blocked_reason()
        if blocked_after is not None:
            return CompanionResult(CompanionStatus.SILENCED, interaction_id, generation,
                                   error_code=blocked_after.upper(), reason_code=blocked_after)
        if result.status == LLMStatus.STALE or result.status in {LLMStatus.CANCELLED}:
            return CompanionResult(CompanionStatus.STALE, interaction_id, generation,
                                   error_code=result.error_code or result.status.value, reason_code="llm_result_stale")
        if result.status != LLMStatus.COMPLETED:
            return CompanionResult(CompanionStatus.FAILED, interaction_id, generation,
                                   error_code=result.error_code or result.status.value, reason_code="llm_failed",
                                   metadata={"latency_s": result.latency_s})
        payload = result.payload if isinstance(result.payload, Mapping) else {}
        response = payload.get("response_text")
        if not isinstance(response, str) or not response.strip():
            return CompanionResult(CompanionStatus.FAILED, interaction_id, generation,
                                   error_code="RESPONSE_TEXT_MISSING", reason_code="invalid_response")
        response = response.strip()[:self.max_output_chars]
        # Optional same-call estimate helps the next turn's strategy without adding a
        # second LLM request. It updates only volatile user-affect context, not traits,
        # permissions, persistent personality, or an anthropomorphic robot mood.
        output_assessment = payload.get("emotion_assessment")
        if isinstance(output_assessment, Mapping):
            observed = emotion.observe(text, model_assessment=output_assessment, source="dialogue_model")
        degraded = bool(payload.get("degraded", False)) or str(result.error_code or "").startswith("DEGRADED_")
        self._append_assistant(interaction_id, generation, response)
        segments = prepare_speech_segments(response, verbosity_chars=self.max_output_chars, bubble_chars=180)
        return CompanionResult(CompanionStatus.DEGRADED if degraded else CompanionStatus.COMPLETED,
                               interaction_id, generation, response, degraded=degraded,
                               error_code=result.error_code, memory_candidate_id=None if candidate is None else candidate.candidate_id,
                               reason_code="local_fallback" if degraded else "dialogue_completed",
                               speech_segments=segments,
                               metadata={"latency_s": result.latency_s, "memory_count": len(memories),
                                         "memory_candidates_before_budget": len(raw_memories),
                                         "dialogue_messages_in_context": len(recent_dialogue),
                                         "context_messages_dropped": budget.dropped_messages,
                                         "context_messages_truncated": budget.truncated_messages,
                                         "memories_dropped_by_budget": budget.dropped_memories,
                                         "dialogue_estimated_tokens": budget.dialogue_tokens,
                                         "memory_estimated_tokens": budget.memory_tokens,
                                         "variable_context_estimated_tokens": context_tokens,
                                         "token_estimate_note": "approximate variable context only; not provider token accounting",
                                         "emotion_estimate": observed.to_dict(), "emotion_strategy": emotion.strategy()})

    def _generation_current(self, interaction_id: str, generation: int) -> bool:
        return self.engine.current_generation(interaction_id) == generation

    def _append_assistant(self, interaction_id: str, generation: int, response: str) -> None:
        if not self._generation_current(interaction_id, generation):
            return
        history = self._history.setdefault(interaction_id, [])
        history.append({"role": "assistant", "content": response, "generation": generation, "timestamp": float(self.clock())})
        history.sort(key=lambda x: (int(x["generation"]), 0 if x["role"] == "user" else 1))
        history[:] = history[-self.max_history_messages:]
        self._persist_turn(interaction_id, history[-1])

    def _persist_turn(self, interaction_id: str, row: Mapping[str, Any]) -> None:
        store = self.session_store
        writer = getattr(store, "save_turn", None) if store is not None else None
        if not callable(writer):
            return
        try:
            writer(interaction_id, str(row["role"]), str(row["content"]),
                   int(row["generation"]), float(row["timestamp"]))
        except Exception:
            # Never claim the transcript is durable when a store failed. In-memory chat
            # can continue; field diagnostics can inspect session-store health separately.
            pass

    def _emotion_for(self, interaction_id: str) -> EmotionContinuity:
        return self._emotions.setdefault(interaction_id, self.emotion if interaction_id == "default" else EmotionContinuity(clock=self.clock))

    def history(self, interaction_id: str = "default") -> list[dict[str, str]]:
        return [{"role": row["role"], "content": row["content"]}
                for row in self._history.get(interaction_id, [])]

    def clear_session(self, interaction_id: str = "default") -> None:
        """Clear only volatile conversational context, not persisted user memories."""
        self._history.pop(interaction_id, None)
        self._loaded_sessions.discard(interaction_id)
        self._emotions.pop(interaction_id, None)
        store = self.session_store
        clearer = getattr(store, "clear_session", None) if store is not None else None
        if callable(clearer):
            clearer(interaction_id)
        if interaction_id == "default":
            self.emotion = EmotionContinuity(clock=self.clock)
            self._emotions[interaction_id] = self.emotion

    def approve_memory_candidate(self, candidate_id: str) -> str:
        return self.memory.approve(candidate_id)

    def reject_memory_candidate(self, candidate_id: str) -> bool:
        return self.memory.reject(candidate_id)

    def correct_memory(self, memory_id: str, corrected_summary: str, *, source_id: str,
                       kind: str | None = None) -> dict[str, Any]:
        return self.memory.correct(memory_id, corrected_summary, source_id=source_id, kind=kind)

    def forget_memory(self, memory_id: str) -> dict[str, Any]:
        return self.memory.forget(memory_id)

    def recall_memories(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        return self.memory.recall(query, limit=limit)
