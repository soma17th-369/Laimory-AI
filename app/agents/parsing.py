"""Event Agent 공통 LLM/파싱 유틸."""

import json
from typing import Protocol

from app.core.llm_stages import LLMStage
from app.core.structured import StructuredOutputError
from app.schemas import AgentEventResult, UserMemory


class SupportsComplete(Protocol):
    """테스트에서 fake 주입이 가능한 LLM 클라이언트 최소 인터페이스."""

    def complete(self, prompt: str, *, system: str | None = ..., **kwargs) -> str: ...

    def complete_json(
        self,
        prompt: str,
        schema=...,
        *,
        system: str | None = ...,
        temperature: float = ...,
        **kwargs,
    ) -> str: ...

    def complete_structured(
        self,
        prompt: str,
        schema,
        *,
        system: str | None = ...,
        temperature: float = ...,
        max_repairs: int = ...,
        **kwargs,
    ): ...


def default_llm(stage: LLMStage | None = None) -> SupportsComplete:
    """설정된 provider로 그 단계에 맞는 LLM 클라이언트를 만든다 (#106).

    `stage` 의 티어에 모델이 지정돼 있으면 그 모델로, 없으면 전역
    `{PROVIDER}_MODEL` 로 만든다. `stage` 를 주지 않으면 언제나 전역 모델이다.

    model 을 **주지 않는 경로를 남겨 둔 것이 중요하다.** `LLMClient()` 는 provider
    싱글턴을 그대로 재사용하므로, 티어 설정이 없는 배포에서는 지금까지와 똑같이
    provider 인스턴스가 하나만 생긴다.

    import 시점에 설정/자격 증명을 요구하지 않도록 함수 안에서 import 한다.
    """

    from app.core.llm import LLMClient
    from app.core.llm_stages import model_for_stage

    model = model_for_stage(stage)
    return LLMClient() if model is None else LLMClient(model=model)


def items_to_text(items: list) -> str:
    """Pydantic 모델 리스트를 프롬프트용 JSON 문자열로 직렬화한다."""

    if not items:
        return "없음"
    dumped = [m.model_dump(by_alias=True, mode="json") for m in items]
    return json.dumps(dumped, ensure_ascii=False, indent=2)


#: 프롬프트에 싣지 않는 키. 좌표는 **코드만** 쓴다(이슈 #80).
_COORDINATE_KEYS = frozenset({"latitude", "longitude"})


def strip_coordinates(payload):
    """직렬화한 payload 에서 위경도를 걷어낸다.

    좌표는 사람이 읽고 판단할 값이 아니다. Agent 가 직접 해석할 일이 없는데도 원본 항목마다
    실려 나가면서 input token 만 차지한다. 좌표가 필요한 판단(연속 MOVEMENT 사이 끝점 거리
    등)은 코드가 `derivedMetrics` 로 계산해 결론만 넘기므로, 원본에서 빼도 근거가 줄지 않는다.

    `MovementItem` 은 `start`/`end` 안에 좌표를 중첩하므로 재귀로 훑는다. 입력 스키마에서
    좌표를 없애는 것이 아니다 — request 로는 그대로 받고 프롬프트에만 싣지 않는다.
    """

    if isinstance(payload, dict):
        return {
            key: strip_coordinates(value)
            for key, value in payload.items()
            if key not in _COORDINATE_KEYS
        }
    if isinstance(payload, list):
        return [strip_coordinates(item) for item in payload]
    return payload


def items_to_text_without_coordinates(items: list) -> str:
    """`items_to_text` 와 같되 위경도만 뺀다.

    위치 원본을 프롬프트에 싣는 곳(Location·Photo Agent)이 함께 쓴다.
    """

    if not items:
        return items_to_text(items)
    return json.dumps(
        strip_coordinates([m.model_dump(by_alias=True, mode="json") for m in items]),
        ensure_ascii=False,
        indent=2,
    )


def user_memory_to_text(user_memory: UserMemory | None) -> str:
    """user memory를 프롬프트용 문자열로 직렬화한다 (#65).

    **Timeline Agent 와 Question Agent 가 이 함수를 쓴다.** Agent 마다 필드를 골라
    쓰거나 다른 형태로 바꾸지 않는다 — 같은 메모리를 보고 서로 다른 문자열을 읽으면
    어느 Agent 가 무엇을 근거로 판단했는지 재현할 수 없다. 갱신 쪽(#64)의 User Memory
    Agent 도 "기존 프로필" 을 같은 projection 으로 읽는다.

    Event Agent 에는 주입하지 않는다. 자기 source 에 대한 사실 보고가 임무이고,
    다섯이 같은 프로필을 읽으면 Timeline 이 그 합의를 독립된 근거로 착각한다.

    projection 규칙(무엇을 싣고 무엇을 빼는지)은 스키마가 갖는다
    (:meth:`~app.schemas.user_memory.UserMemory.prompt_payload`). 여기서는 직렬화만
    한다. 채워진 필드가 하나도 없으면 User Memory 가 아예 없을 때와 **같은 문자열**을
    돌려준다. 그 둘은 Agent 에게 구분할 이유가 없는 상태다.
    """

    if user_memory is None:
        return "정보 없음"
    payload = user_memory.prompt_payload()
    if not payload:
        return "정보 없음"
    return json.dumps(payload, ensure_ascii=False)


def build_infer_prompt(
    data_text: str,
    *,
    date: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> str:
    """추론 요청(user prompt): 요청 메타데이터 + 분석 데이터.

    User Memory 는 여기 들어가지 않는다(#65). Event Agent 는 자기 source 가 말해 주는
    사실을 보고하는 자리이고, 프로필로 하는 해석은 Timeline 계층의 몫이다.

    시각은 수집 원본이 ISO 문자열이므로 window 도 문자열로 받는다.
    """

    metadata = []
    if date is not None:
        metadata.append(f"date: {date}")
    if window_start is not None:
        metadata.append(f"windowStart: {window_start}")
    if window_end is not None:
        metadata.append(f"windowEnd: {window_end}")
    metadata_text = "\n".join(metadata) if metadata else "정보 없음"

    return (
        f"[요청 메타데이터]\n{metadata_text}\n\n"
        f"[분석할 데이터]\n{data_text}\n\n"
        "이 데이터가 생긴 원인이 되었을 법한 사용자의 일상 event 후보(candidates)와, "
        "후보보다 불확실하지만 하루 event일 가능성이 있는 단서(fragments)를 과감하게 추론해 "
        "지정된 JSON 형식으로 출력하세요. "
        "출력 시간은 반드시 요청 date/window와 입력 timestamp에 맞는 KST(+09:00) ISO 8601이어야 합니다. "
        "요청 window(windowStart~windowEnd)는 엄격한 경계입니다. 후보/단서의 시간은 반드시 이 window 안에 있어야 하며, "
        "window를 벗어난 입력은 event로 만들지 않습니다. window 경계에 걸치는 입력은 window 안쪽 구간만 사용합니다."
    )


def parse_agent_result(text: str) -> AgentEventResult:
    """LLM JSON 응답을 ``AgentEventResult``로 파싱하고 검증한다.

    코드펜스나 앞뒤 텍스트가 있어도 첫 ``{``부터 마지막 ``}``까지를 잘라 파싱한다.
    Pydantic 스키마 검증까지 통과해야 하므로 잘못된 enum/필드는 걸러진다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
    payload = json.loads(text[start : end + 1])
    return AgentEventResult.model_validate(payload)
