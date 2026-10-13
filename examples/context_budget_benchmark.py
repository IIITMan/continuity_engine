"""Offline, privacy-safe estimate of context trimming on an authored fixture.

This script does not call an LLM and does not measure provider latency or billed tokens.
It compares variable-context estimates only; use live-provider accounting for true costs.
"""
from __future__ import annotations

import json

from continuity_engine import DialogueContextBudgeter, estimate_tokens


def main() -> None:
    dialogue = []
    for index in range(12):
        role = "user" if index % 2 == 0 else "assistant"
        content = (
            f"对话轮次{index}：我们在讨论长期陪伴、考试准备、最近的压力、生活安排，"
            "以及之前提过的兴趣。请延续之前的重点，同时尊重用户当前的表达和偏好。"
        )
        dialogue.append({"role": role, "text": content})
    # Baseline: twelve persisted/history messages include the current user turn, while
    # that same turn is also passed separately as `user_input` (the previous behavior).
    current_text = "这一次我想继续聊考试准备和最近的压力，记得我们之前谈过的重点吗？"
    old_history_window = (dialogue + [{"role": "user", "text": current_text}])[-12:]
    memories = [
        {"memory_id": f"memory-{index}", "kind": "user_fact",
         "summary": (f"长期记忆{index}：用户曾提到关于考试准备、个人兴趣、生活偏好和重要安排的事实。" * 3),
         "source_refs": [f"turn-{index}"]}
        for index in range(5)
    ]
    before_dialogue_tokens = sum(estimate_tokens(row["text"]) for row in old_history_window)
    current_input_tokens = estimate_tokens(current_text)
    before_memory_tokens = sum(estimate_tokens(row["summary"]) + estimate_tokens(row["kind"]) + 8 for row in memories)
    # The optimized path keeps only prior history; current_text is supplied exactly once.
    budget = DialogueContextBudgeter().prepare(old_history_window[:-1], memories)
    after_dialogue_tokens = budget.dialogue_tokens
    after_memory_tokens = budget.memory_tokens
    before = before_dialogue_tokens + current_input_tokens + before_memory_tokens
    after = after_dialogue_tokens + current_input_tokens + after_memory_tokens
    print(json.dumps({
        "mode": "offline_authored_fixture",
        "provider_called": False,
        "dialogue_messages_before": len(old_history_window),
        "dialogue_messages_after": len(budget.dialogue),
        "current_user_input_estimated_tokens": current_input_tokens,
        "current_user_input_duplicated_in_baseline": True,
        "current_user_input_duplicated_after_optimization": False,
        "memories_before": len(memories),
        "memories_after": len(budget.memories),
        "estimated_variable_context_tokens_before": before,
        "estimated_variable_context_tokens_after": after,
        "estimated_reduction_percent": round((1.0 - after / before) * 100.0, 2) if before else 0.0,
        "dialogue_tokens_after": after_dialogue_tokens,
        "memory_tokens_after": after_memory_tokens,
        "dropped_messages": budget.dropped_messages,
        "truncated_messages": budget.truncated_messages,
        "dropped_memories": budget.dropped_memories,
        "note": "rough budget estimator, not provider tokenizer usage or live latency",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
