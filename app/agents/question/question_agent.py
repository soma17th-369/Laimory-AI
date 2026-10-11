"""Question Agent (이슈 #66).

확정된 타임라인의 event 를 보고, 사용자가 자기 경험을 덧붙이도록 이끄는 **회고
유도 질문**을 만든다. Laimory 의 타임라인은 완성된 기록이 아니라 사용자가 채워
나갈 초안이고, 센서와 일정이 말해 주지 못하는 생각·감정·이유를 꺼내는 것이 이
에이전트의 일이다.

## 왜 Repair 뒤인가

Repair 가 event 를 병합·삭제하고 `clientEventId` 를 다시 매기기 때문에, 그 전에
만든 질문은 대상이 사라지거나 다른 event 를 가리키게 된다. 그래서 이 단계는
**항상 Repair 다음, 결과 저장 이전**이다. 질문 하나가 event 하나에 붙고, 그 참조는
저장 계약에서 중첩(`TimelineResultEvent.question`)으로 표현된다.

## 모든 event 에 하나씩

예외를 두지 않는다. 결과의 event 는 전부 질문 하나를 갖는다. 물을 것이 마땅치 않아
보이는 event 도 마찬가지다 — 사용자가 그 시간에 대해 남길 말이 있는지는 우리가 아니라
사용자가 판단한다.

1차 응답에서 빠진 event 가 있으면 그것만 모아 **한 번** 다시 묻는다. 두 번째도 비면
그 event 의 질문은 ``None`` 이고 draft 에 warning 이 남는다. 보장은 최선을 다하되
저장을 막지는 않는다.

## 무엇을 LLM 에게 맡기고 무엇을 코드가 정하는가

파이프라인의 다른 곳과 같은 분담이다.

- **LLM**: 어떤 문장으로 물을지 (의미 판단)
- **코드**: 얼마나 길 수 있는지, 어떤 응답을 버릴지, 빠진 것을 다시 물을지
  (셀 수 있는 것)

## 입력에서 빼는 것

`confidence`·`inferenceLevel`·`uncertainty`·`sourceRefs` 는 프롬프트에 **넣지
않는다**. 시스템 정보를 질문에 노출하지 말라고 지시하는 것보다, 애초에 주지 않는
편이 확실하다. 모델은 없는 것을 인용할 수 없다.

## 실패

질문은 타임라인 본문에 얹는 부가 가치라, 실패해도 하루 기록 전체를 버리지 않는다.
호출부(main agent)가 예외를 흡수하고 질문 없는 draft 를 그대로 흘려보낸다. 이
모듈은 자기 실패를 삼키지 않고 그대로 올린다 — 코드 부여와 기록은 흡수하는 쪽의
몫이다.
"""

import json

from pydantic import Field, ValidationError

from app.agents.base import Agent
from app.agents.parsing import SupportsComplete, default_llm, user_memory_to_text
from app.agents.prompt_loader import load_prompt
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.logging import get_logger, log_fields
from app.core.structured import StructuredOutputError
from app.core.llm_stages import LLMStage
from app.schemas import (
    CamelModel,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineEventDraft,
    TimelineWarning,
    TimelineWarningSeverity,
)

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt(__file__, "question.md")

#: 질문 한 개의 하드 상한(저장 계약과 같은 값). 넘는 질문은 자르지 않고 **버린다**
#: — 잘린 질문은 문장이 끝나지 않아 물음이 되지 못한다.
_MAX_QUESTION_LENGTH = 255


class EventQuestion(CamelModel):
    """LLM 이 돌려주는 질문 한 건."""

    client_event_id: str = Field(alias="clientEventId", min_length=1)
    question: str = Field(min_length=1)


class QuestionSet(CamelModel):
    """Question Agent 응답 계약."""

    questions: list[EventQuestion] = Field(default_factory=list)


def build_question_prompt(
    request: TimelineDraftRequest,
    events: list[dict],
    *,
    retry: bool = False,
) -> str:
    """질문 생성 user prompt 를 만든다.

    ``retry`` 는 1차 응답에서 질문을 받지 못한 event 만 다시 묻는 경우다. 같은
    지시를 반복하는 대신 "빠뜨렸다" 는 사실을 알려 준다.

    User Memory 를 함께 준다(#65). 무엇을 물을지가 아니라 **어떻게 물을지**를 고르는
    자료다 — 같은 event 라도 사람마다 남기고 싶은 말이 다르고, 그 결이
    ``memoryStyle``·``emotionalPatterns``·``preferences`` 에 있다. Timeline 과 같은
    projection 을 쓴다.
    """

    tail = (
        "아래 event 들은 앞선 요청에서 질문을 받지 못했습니다. "
        "빠짐없이 하나씩 다시 만들어 주세요."
        if retry
        else "event 는 사용자가 오늘 실제로 겪은 일입니다."
    )
    return (
        f"[date]\n{request.date}\n\n"
        f"[user memory]\n{user_memory_to_text(request.user_memory)}\n\n"
        f"[events]\n{json.dumps(events, ensure_ascii=False, indent=2)}\n\n"
        f"{tail} "
        "**모든 event 에 질문을 하나씩** 만드세요. 건너뛰는 event 가 있으면 안 됩니다. "
        f"입력 event 가 {len(events)}개이므로 질문도 {len(events)}개여야 합니다."
    )


def project_event(event: TimelineEventDraft) -> dict:
    """질문 생성에 필요한 최소 정보만 남긴다.

    시각은 분을 버리고 시 단위로만 준다. 질문에 `13시 42분` 같은 센서 수치가 새는
    것을 막는 가장 확실한 방법은 그 값을 주지 않는 것이다(#61 의 문장 계약과 같은
    이유다).
    """

    projected: dict[str, object] = {
        "clientEventId": event.client_event_id,
        "eventType": event.event_type.value,
        "title": event.title,
        "timeOfDay": f"{event.start_time.hour}시~{event.end_time.hour}시",
    }
    if event.description:
        projected["description"] = event.description
    if event.place:
        projected["place"] = event.place
    return projected


def parse_questions(text: str) -> list[EventQuestion]:
    """LLM 응답에서 질문 목록을 뽑는다(부분 손상에 강하게).

    항목 하나가 깨져도 나머지 질문은 살린다. JSON 자체를 찾지 못한 경우만 예외로
    올려 호출부의 흡수 경로로 넘긴다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")

    payload = json.loads(text[start : end + 1])

    questions: list[EventQuestion] = []
    for index, raw in enumerate(payload.get("questions") or [], start=1):
        if not isinstance(raw, dict):
            continue
        try:
            questions.append(EventQuestion.model_validate(raw))
        except ValidationError as exc:
            report_error(
                logger,
                ErrorCode.STRUCTURED_OUTPUT_INVALID,
                "질문 항목 검증 실패로 제외",
                exc=exc,
                context={"index": index, "action": "skipped"},
                # 질문 항목마다 부른다. 빠진 질문은 재요청 뒤 warning 이 답한다.
                emit=False,
            )
    return questions


class QuestionAgent(Agent):
    """확정된 타임라인 event 에 회고 유도 질문을 붙인다."""

    name = "question"

    def __init__(self, llm: SupportsComplete | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> SupportsComplete:
        if self._llm is None:
            self._llm = default_llm(LLMStage.QUESTION)
        return self._llm

    def generate(
        self,
        request: TimelineDraftRequest,
        draft: TimelineDraft,
    ) -> TimelineDraft:
        """draft 의 **모든 event** 에 ``question`` 을 채워 같은 draft 를 돌려준다.

        event 가 없으면 LLM 을 호출하지 않는다. 1차 응답에서 빠진 event 가 있으면
        그것만 모아 **한 번** 다시 묻는다. 두 번째도 비면 그 event 는 ``None`` 으로
        남기고 warning 을 붙인다 — 질문 하나 때문에 하루 기록을 막지 않는다.

        1차 호출 실패는 흡수하지 않고 올린다(호출부가 draft 를 지킨다). 반면 재요청
        실패는 삼킨다. 이미 받아 둔 1차 질문까지 버릴 이유가 없다.
        """

        if not draft.events:
            logger.debug("event 가 없어 질문 생성을 건너뜁니다.")
            return draft

        self._ask(request, draft, draft.events)

        missing = [event for event in draft.events if event.question is None]
        if missing:
            try:
                self._ask(request, draft, missing, retry=True)
            except Exception as exc:  # noqa: BLE001 - 1차 결과를 지킨다
                report_error(
                    logger,
                    ErrorCode.QUESTION_GENERATION_FAILED,
                    "기록 질문 재요청 실패",
                    exc=exc,
                    context={"missingCount": len(missing), "action": "kept_partial"},
                )

        unanswered = [event for event in draft.events if event.question is None]
        if unanswered:
            draft.warnings.append(
                TimelineWarning(
                    warning_id="warning-question-missing-001",
                    severity=TimelineWarningSeverity.LOW,
                    message=(
                        f"{len(unanswered)}개 이벤트의 기록 질문을 만들지 못했습니다."
                    ),
                )
            )
            logger.info(
                "기록 질문이 비어 있는 event 가 남았습니다.",
                extra=log_fields(
                    eventCount=len(draft.events),
                    unansweredCount=len(unanswered),
                ),
            )
        return draft

    def _ask(
        self,
        request: TimelineDraftRequest,
        draft: TimelineDraft,
        events: list[TimelineEventDraft],
        *,
        retry: bool = False,
    ) -> None:
        """``events`` 에 대해 질문을 받아 draft 에 적용한다."""

        prompt = build_question_prompt(
            request,
            [project_event(event) for event in events],
            retry=retry,
        )
        text = self.llm.complete_json(
            prompt, QuestionSet, system=_SYSTEM_PROMPT, temperature=0.4
        )
        self._apply(draft, parse_questions(text), retry=retry)

    def _apply(
        self,
        draft: TimelineDraft,
        questions: list[EventQuestion],
        *,
        retry: bool = False,
    ) -> None:
        """질문을 event 에 붙인다. 규칙을 어긴 질문은 조용히 버리지 않고 남긴다.

        - 알 수 없는 ``clientEventId`` 는 버린다. 사라진 event 를 되살리지 않는다.
        - 이미 질문이 붙은 event 는 건드리지 않는다(같은 event 에 둘 이상이면 첫 질문).
        - 물음표로 끝나지 않거나 길이 상한을 넘는 질문은 버린다.
        """

        by_id = {event.client_event_id: event for event in draft.events}
        skipped: dict[str, int] = {}
        applied = 0

        for item in questions:
            event = by_id.get(item.client_event_id)
            if event is None:
                skipped["unknownEvent"] = skipped.get("unknownEvent", 0) + 1
                continue
            if event.question is not None:
                skipped["duplicate"] = skipped.get("duplicate", 0) + 1
                continue

            question = item.question.strip()
            if not question.endswith("?"):
                skipped["notAQuestion"] = skipped.get("notAQuestion", 0) + 1
                continue
            if len(question) > _MAX_QUESTION_LENGTH:
                skipped["tooLong"] = skipped.get("tooLong", 0) + 1
                continue

            event.question = question
            applied += 1

        if skipped:
            # 질문 본문은 남기지 않는다. 사용자가 읽을 문장이고, 사유별 개수만
            # 있으면 "모델이 어떤 규칙을 계속 어기는지" 는 충분히 보인다.
            logger.info(
                "기록 질문 일부를 제외했습니다.",
                extra=log_fields(applied=applied, retry=retry, **skipped),
            )
