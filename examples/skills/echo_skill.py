"""Reference skill implementation for the Continuity Engine skill contract.

Run from the project root with:
    PYTHONPATH=src python examples/skills/echo_skill.py
"""
from __future__ import annotations

from typing import Any, Mapping

from continuity_engine import SkillCall, SkillRegistry, SkillResult, SkillSpec, SkillStatus


class EchoSkill:
    """A safe reference skill: validates input and returns data without side effects."""

    async def execute(self, call: SkillCall) -> SkillResult:
        text = call.arguments.get("text", "")
        if not isinstance(text, str):
            return SkillResult(
                status=SkillStatus.FAILED,
                skill_id=call.skill_id,
                request_id=call.request_id,
                error_code="INVALID_ARGUMENT_TEXT",
                message="参数 text 必须是字符串。",
            )
        if len(text) > 500:
            return SkillResult(
                status=SkillStatus.FAILED,
                skill_id=call.skill_id,
                request_id=call.request_id,
                error_code="ARGUMENT_TOO_LONG",
                message="参数 text 超过 500 个字符。",
            )
        return SkillResult(
            status=SkillStatus.OK,
            skill_id=call.skill_id,
            request_id=call.request_id,
            data={"echo": text},
        )


async def main() -> None:
    registry = SkillRegistry()
    await registry.register(
        SkillSpec(
            skill_id="example.echo",
            description="回显经过校验的文本，不产生外部副作用。",
            required_permissions=(),
            max_concurrency=1,
        ),
        EchoSkill(),
    )
    result = await registry.dispatch(SkillCall("example.echo", {"text": "你好"}))
    print(result.status.value, dict(result.data))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
