"""Deterministic, privacy-neutral budgets for dialogue and retrieved memories.

The estimator is intentionally conservative and dependency-free. It is not a provider
 tokenizer; provider-reported usage remains authoritative when available. This module
 limits variable context before building the strict LLM state view, reducing duplicate
 current-turn text and preventing old transcripts or long memories from growing prompts
 without bound.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Mapping, Sequence

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """Return a conservative rough token estimate for mixed CJK/Latin text.

    CJK code points are budgeted at one token each; non-CJK non-whitespace characters
    at roughly one token per 3.5 characters. A 10% safety margin avoids treating this as
    a hard provider tokenizer. The value is for budgeting/relative comparison only.
    """
    if not isinstance(text, str) or not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = sum(1 for char in text if not char.isspace() and not _CJK_RE.match(char))
    base = cjk + non_cjk / 3.5
    return int(math.ceil(base * 1.10))


def _trim_middle(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text
    marker = " …[内容已压缩]… "
    lo, hi = 0, len(text)
    best = marker.strip()
    # Find the longest balanced head/tail selection fitting the target budget.
    while lo <= hi:
        keep = (lo + hi) // 2
        head = keep // 2
        tail = keep - head
        candidate = (text[:head] + marker + text[-tail:]) if tail else (text[:head] + marker)
        if estimate_tokens(candidate) <= token_budget:
            best = candidate
            lo = keep + 1
        else:
            hi = keep - 1
    return best


@dataclass(frozen=True, slots=True)
class ContextBudgetResult:
    dialogue: tuple[dict[str, str], ...]
    memories: tuple[dict[str, Any], ...]
    dialogue_tokens: int
    memory_tokens: int
    dropped_messages: int
    truncated_messages: int
    dropped_memories: int

    @property
    def variable_context_tokens(self) -> int:
        return self.dialogue_tokens + self.memory_tokens


class DialogueContextBudgeter:
    """Bound recent dialogue while preserving useful older context where possible.

    Oversized messages are middle-trimmed to the remaining per-message budget. They do
    not terminate the backward scan: doing so made a single long assistant response
    discard every earlier message, including short user-stated facts that were still
    within the overall dialogue budget.
    """

    def __init__(self, *, max_dialogue_tokens: int = 700,
                 max_memory_tokens: int = 300, max_dialogue_messages: int = 8,
                 max_message_tokens: int = 220, max_assistant_message_tokens: int = 120,
                 max_memory_items: int = 3,
                 max_memory_summary_chars: int = 240):
        for name, value, minimum, maximum in (
            ("max_dialogue_tokens", max_dialogue_tokens, 128, 16000),
            ("max_memory_tokens", max_memory_tokens, 64, 8000),
            ("max_dialogue_messages", max_dialogue_messages, 1, 12),
            ("max_message_tokens", max_message_tokens, 32, 4000),
            ("max_assistant_message_tokens", max_assistant_message_tokens, 32, 4000),
            ("max_memory_items", max_memory_items, 0, 20),
            ("max_memory_summary_chars", max_memory_summary_chars, 32, 2000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be in [{minimum},{maximum}]")
        self.max_dialogue_tokens = max_dialogue_tokens
        self.max_memory_tokens = max_memory_tokens
        self.max_dialogue_messages = max_dialogue_messages
        self.max_message_tokens = max_message_tokens
        self.max_assistant_message_tokens = max_assistant_message_tokens
        self.max_memory_items = max_memory_items
        self.max_memory_summary_chars = max_memory_summary_chars

    def fit_dialogue(self, rows: Sequence[Mapping[str, Any]] | None) -> tuple[tuple[dict[str, str], ...], int, int, int]:
        clean: list[dict[str, str]] = []
        if rows:
            for row in rows:
                if not isinstance(row, Mapping) or row.get("role") not in {"user", "assistant"}:
                    continue
                text = row.get("text", row.get("content", ""))
                if not isinstance(text, str) or not text.strip():
                    continue
                clean.append({"role": str(row["role"]), "text": text.strip()})
        original_count = len(clean)
        selected: list[dict[str, str]] = []
        used = 0
        truncated = 0
        for item in reversed(clean[-self.max_dialogue_messages:]):
            text = item["text"]
            # Preserve room for user statements, which are the primary source of
            # conversational facts. Long assistant prose is useful, but should not
            # consume the entire rolling-context window and evict the user's earlier
            # goal or preference. Keep this independent of the global safety ceiling.
            role_budget = (min(self.max_message_tokens, self.max_assistant_message_tokens)
                           if item["role"] == "assistant" else self.max_message_tokens)
            item_budget = min(role_budget, self.max_dialogue_tokens - used)
            if item_budget <= 0:
                break
            cost = estimate_tokens(text)
            was_truncated = False
            if cost > item_budget:
                # Trim any oversized message, not only the newest one. The previous
                # `break` here made one long older assistant reply discard all still
                # older context, even when budget remained. In real Chinese dialogue,
                # a 250–400 character reply regularly exceeded the old 220-token cap.
                text = _trim_middle(text, item_budget)
                was_truncated = True
                cost = estimate_tokens(text)
            if cost > item_budget:
                # A tiny remaining budget may not fit even the truncation marker. Skip
                # this message and keep scanning older rows; an older short fact may
                # still fit. The final result remains globally bounded.
                continue
            if was_truncated:
                truncated += 1
            selected.append({"role": item["role"], "text": text})
            used += cost
            if len(selected) >= self.max_dialogue_messages:
                break
        selected.reverse()
        return tuple(selected), used, max(0, original_count - len(selected)), truncated

    def fit_memories(self, rows: Sequence[Mapping[str, Any]] | None) -> tuple[tuple[dict[str, Any], ...], int, int]:
        clean: list[dict[str, Any]] = []
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            memory_id, summary = row.get("memory_id"), row.get("summary")
            if not isinstance(memory_id, str) or not memory_id or not isinstance(summary, str) or not summary.strip():
                continue
            kind = row.get("kind", "unknown")
            refs = row.get("source_refs", [])
            if not isinstance(kind, str):
                kind = "unknown"
            if not isinstance(refs, (list, tuple)):
                refs = []
            clean.append({"memory_id": memory_id[:200], "summary": summary.strip()[:self.max_memory_summary_chars],
                          "source_refs": [str(ref)[:200] for ref in refs[:5] if isinstance(ref, str)],
                          "kind": kind[:100]})
        selected: list[dict[str, Any]] = []
        used = 0
        for item in clean[:self.max_memory_items]:
            cost = estimate_tokens(item["summary"]) + estimate_tokens(item["kind"]) + 8
            if used + cost <= self.max_memory_tokens:
                selected.append(item)
                used += cost
                continue
            remaining = self.max_memory_tokens - used - estimate_tokens(item["kind"]) - 8
            if remaining >= 24 and len(selected) < self.max_memory_items:
                summary = _trim_middle(item["summary"], remaining)
                if summary.strip():
                    item = dict(item)
                    item["summary"] = summary
                    selected.append(item)
                    used += estimate_tokens(summary) + estimate_tokens(item["kind"]) + 8
            break
        return tuple(selected), used, max(0, len(clean) - len(selected))

    def prepare(self, dialogue: Sequence[Mapping[str, Any]] | None,
                memories: Sequence[Mapping[str, Any]] | None) -> ContextBudgetResult:
        fitted_dialogue, dialogue_tokens, dropped_messages, truncated_messages = self.fit_dialogue(dialogue)
        fitted_memories, memory_tokens, dropped_memories = self.fit_memories(memories)
        return ContextBudgetResult(fitted_dialogue, fitted_memories, dialogue_tokens, memory_tokens,
                                   dropped_messages, truncated_messages, dropped_memories)
