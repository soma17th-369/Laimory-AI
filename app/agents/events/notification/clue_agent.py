"""알림 단서 Agent (v4 세트, #169).

입력 구성은 v3 와 같다(`build_notification_payload`: 정책·대화 묶음·미분류). 출력이 다르다 —
candidate·fragment·eventType 대신, 같은 일을 가리키는 알림 묶음마다 **무슨 내용인지(종류),
상대·발신자, 민감정보를 가린 요약, 알림이 말하는 일정이 오늘인지**만 낸다. 무엇이 하루의
사건인지, 대화 3개를 고르는 것은 Timeline v4 와 `conversation_guard` 가 한다.

**필요한 알림만 남긴다(#173, 사용자 결정 2026-10-31).** 뒤 단계에 넘기는 것은 다섯 종류에 분명히
드는 알림뿐이고, 나머지는 모델이 내용을 읽고 `dropped` 에 id 만 적는다. 이 Agent 가 알림을 고르는
자리다 — 광고까지 단서로 넘기면 Timeline 과 Repair 매 반복의 입력이 된다.

내용 읽기와 마스킹은 의미 판단이라 LLM 에 남긴다. 코드가 하는 것은 셋이다.

- 알림·대화 묶음을 짧은 id(`n3`·`g1`)로 싣고 돌려받는다. UUID 를 옮겨 적게 하지 않는다.
- 남긴 알림은 단서 하나에 정확히 한 번 들어가게 한다. 두 번 나온 알림은 처음 것만 둔다.
  `clues` 와 `dropped` 양쪽에 나온 알림은 남긴다. 어느 쪽에도 없는 알림은 모델이 읽지 못한 것이라
  버리되 건수를 warning 으로 남긴다.
- 요약·상대에 남은 민감정보 모양(`SENSITIVE_PATTERNS`)을 찾으면 그 값을 지운다. 한 번 새면
  최종 일기에 남으므로 프롬프트만 믿지 않는다.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from itertools import count

from app.agents.events.notification.agent import build_notification_payload
from app.agents.parsing import SupportsComplete, build_infer_prompt, default_llm
from app.agents.prompt_loader import load_prompt
from app.core.error_codes import ErrorCode, message_for
from app.core.exceptions import code_of_or, report_error
from app.core.execution_context import ExecutionStage
from app.core.llm_stages import LLMStage
from app.core.logging import get_logger, stage_span
from app.schemas import AgentWarning, NotificationItem, TimelineDraftRequest
from app.schemas.clues import NotificationClue, NotificationClueKind, NotificationClueSet
from app.services.notification_guard import SENSITIVE_PATTERNS
from app.services.validator import parse_datetime, resolve_timezone

logger = get_logger(__name__)


@cache
def _system_prompt() -> str:
    return load_prompt(__file__, "prompt.md")


@dataclass
class NotificationClueResult:
    clues: list[NotificationClue] = field(default_factory=list)
    warnings: list[AgentWarning] = field(default_factory=list)


@dataclass(frozen=True)
class _Payload:
    """프롬프트 입력과, 짧은 id 가 가리키는 알림."""

    body: dict
    members: dict[str, list[NotificationItem]]


class NotificationClueAgent:
    """알림을 읽어 알림 단서를 만든다."""

    name = "notification"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.NOTIFICATION)
        return self._llm

    def generate(self, request: TimelineDraftRequest) -> NotificationClueResult:
        items = list(request.notifications)
        if not items:
            return NotificationClueResult()

        with stage_span(logger, ExecutionStage.EVENT_AGENT, agent=self.name) as outcome:
            payload = build_notification_clue_payload(items)
            warnings: list[AgentWarning] = []
            try:
                output = self._read(payload, request)
            except Exception as exc:
                failure_code = code_of_or(exc, ErrorCode.EVENT_AGENT_FAILED)
                report_error(
                    logger,
                    failure_code,
                    "알림 단서 해석 실패",
                    exc=exc,
                    context={"agent": self.name, "fallback": "no_clues"},
                    exc_info=True,
                )
                warnings.append(
                    AgentWarning(
                        agent_name=self.name,
                        message=f"알림 해석 실패: {message_for(failure_code)}. 알림 없이 진행합니다.",
                    )
                )
                output = None
            clues, stats = assemble_notification_clues(output, payload, request)
            if output is not None and stats["unread"]:
                warnings.append(
                    AgentWarning(
                        agent_name=self.name,
                        message=f"응답에서 빠진 알림 {stats['unread']}묶음을 버렸습니다.",
                    )
                )
            if stats["masked"]:
                warnings.append(
                    AgentWarning(
                        agent_name=self.name,
                        message=f"민감정보 모양이 남은 요약 {stats['masked']}건의 내용을 지웠습니다.",
                    )
                )
            outcome["clueCount"] = len(clues)
            outcome.update({f"{key}Count": value for key, value in stats.items()})
            return NotificationClueResult(clues=clues, warnings=warnings)

    def _read(self, payload: _Payload, request: TimelineDraftRequest) -> NotificationClueSet:
        prompt = build_infer_prompt(
            json.dumps(payload.body, ensure_ascii=False, indent=2),
            date=request.date,
            window_start=request.window.start if request.window else None,
            window_end=request.window.end if request.window else None,
        )
        return self.llm.complete_structured(
            prompt, NotificationClueSet, system=_system_prompt(), temperature=0.2
        )


def build_notification_clue_payload(items: list[NotificationItem]) -> _Payload:
    """v3 입력 구성에서 rawId 를 짧은 id 로 바꾼다.

    결제·예약 앱과 사전에 없는 앱의 알림은 한 건마다 `n1`, `n2` …, 대화 묶음은 묶음마다
    `g1`, `g2` … 다. 대화 묶음은 묶음 하나가 대화 하나라 메시지에는 id 를 달지 않는다.
    """

    by_raw_id = {str(item.raw_id): item for item in items}
    body = build_notification_payload(items)
    members: dict[str, list[NotificationItem]] = {}
    numbers = count(1)

    def single(entry: dict) -> dict:
        short_id = f"n{next(numbers)}"
        members[short_id] = [by_raw_id[str(entry["rawId"])]]
        return {"id": short_id, **{k: v for k, v in entry.items() if k != "rawId"}}

    notifications = [single(entry) for entry in body["notifications"]]
    unclassified = [single(entry) for entry in body["unclassified"]]
    conversations = []
    for number, conversation in enumerate(body["conversations"], start=1):
        group_id = f"g{number}"
        members[group_id] = [by_raw_id[str(m["rawId"])] for m in conversation["messages"]]
        conversations.append(
            {
                "id": group_id,
                **{k: v for k, v in conversation.items() if k != "messages"},
                "messages": [
                    {"postedAt": m["postedAt"], "text": m["text"]}
                    for m in conversation["messages"]
                ],
            }
        )
    return _Payload(
        body={
            "policies": body["policies"],
            "notifications": notifications,
            "conversations": conversations,
            "unclassified": unclassified,
        },
        members=members,
    )


def assemble_notification_clues(
    result: NotificationClueSet | None,
    payload: _Payload,
    request: TimelineDraftRequest,
) -> tuple[list[NotificationClue], dict[str, int]]:
    """모델 출력을 알림 단서로 만든다. 남긴 알림은 단서 하나에 정확히 한 번 들어간다.

    `result` 가 없으면 호출이 실패한 것이다. 단서 없이 진행한다.
    """

    tz = resolve_timezone(request.timezone)
    used: set[str] = set()
    drafts = []
    stats = {"unread": 0, "dropped": 0, "masked": 0, "duplicate": 0, "unknownId": 0}

    for output in result.clues if result is not None else []:
        group: list[NotificationItem] = []
        for short_id in output.item_ids:
            if short_id not in payload.members:
                stats["unknownId"] += 1
                continue
            if short_id in used:
                stats["duplicate"] += 1
                continue
            used.add(short_id)
            if output.kind is NotificationClueKind.PROMOTION:
                stats["dropped"] += 1
                continue
            group.extend(payload.members[short_id])
        if group:
            drafts.append((output, group))

    for short_id in result.dropped if result is not None else []:
        if short_id in payload.members and short_id not in used:
            used.add(short_id)
            stats["dropped"] += 1
    stats["unread"] = len(payload.members.keys() - used)

    rows = []
    for output, group in drafts:
        posted = sorted(
            dt for item in group if (dt := parse_datetime(item.posted_at, tz)) is not None
        )
        summary = output.summary.strip() if output.summary else None
        counterpart = output.counterpart.strip() if output.counterpart else None
        title = output.title.strip() if output.title else None
        if _sensitive(summary) or _sensitive(counterpart) or _sensitive(title):
            stats["masked"] += 1
            summary, counterpart, title = None, None, None
        rows.append(
            dict(
                kind=output.kind,
                raw_ids=[str(item.raw_id) for item in group],
                app_names=_unique(item.app_name for item in group),
                counterpart=counterpart or None,
                summary=summary or None,
                title=title or None,
                schedule_today=output.schedule_today,
                first_at=posted[0] if posted else None,
                last_at=posted[-1] if posted else None,
            )
        )
    rows.sort(key=lambda row: (row["first_at"] is None, _sort_key(row["first_at"])))
    clues = [
        NotificationClue(clue_id=f"N{number}", **row) for number, row in enumerate(rows, start=1)
    ]
    return clues, stats


def _sensitive(text: str | None) -> bool:
    return bool(text) and any(pattern.search(text) for _, pattern in SENSITIVE_PATTERNS)


def _unique(values) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _sort_key(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0
