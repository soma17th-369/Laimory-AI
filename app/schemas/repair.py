"""Repair Agent 계약.

Repair Agent 는 draft 를 직접 다시 써 내려가지 않는다. 확정된 draft 를 보고
**무엇이 문제인지(`issues`)** 와 **무엇을 할 것인지(`toolCalls`)** 만 말하고, 실제
수정은 코드(도구)가 한다. LLM 에게 draft 전체를 다시 쓰게 하면 손대지 않기로 한
event 까지 조용히 바뀌기 때문이다.

`toolCalls` 의 `tool` 이름과 `args` 형태는 `app/agents/repair/tools.py` 의 도구
카탈로그가 정의하고, 그 카탈로그가 그대로 프롬프트에 실린다.
"""

from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.timeline import TimelineWarningSeverity


class RepairIssue(CamelModel):
    """Repair Agent 가 찾아낸 draft 의 문제 하나."""

    client_event_id: str | None = Field(
        default=None,
        alias="clientEventId",
        description="문제가 걸린 event. draft 전체의 문제면 비운다.",
    )
    problem: str = Field(min_length=1, description="무엇이 왜 잘못되었는지")
    severity: TimelineWarningSeverity = TimelineWarningSeverity.MEDIUM


class RepairToolCall(CamelModel):
    """개선 계획의 한 단계. 도구 이름과 인자로만 표현한다."""

    tool: str = Field(min_length=1, description="도구 카탈로그의 도구 이름")
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", description="이 도구를 호출하는 이유")


class RepairPlan(CamelModel):
    """한 번의 분석이 내놓는 문제 목록 + 개선 실행 계획.

    `done` 이 참이거나 `toolCalls` 가 비어 있으면 더 고칠 것이 없다는 뜻이고,
    Repair Agent 는 반복을 끝낸다.
    """

    issues: list[RepairIssue] = Field(default_factory=list)
    tool_calls: list[RepairToolCall] = Field(default_factory=list, alias="toolCalls")
    done: bool = Field(default=False, description="더 고칠 것이 없으면 true")
    summary: str = Field(default="", description="이번 분석 요약")


class RepairToolResult(CamelModel):
    """도구 실행 결과. 다음 분석 프롬프트에 그대로 실려 LLM 이 결과를 보게 한다."""

    tool: str = Field(min_length=1)
    ok: bool = True
    message: str = Field(default="")
