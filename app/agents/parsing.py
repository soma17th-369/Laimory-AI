"""Event Agent 공통 LLM/파싱 유틸."""

import json
from typing import Protocol

from app.schemas import AgentEventResult, UserMemory


class SupportsComplete(Protocol):
    """테스트에서 fake 주입이 가능한 LLM 클라이언트 최소 인터페이스."""

    def complete(self, prompt: str, *, system: str | None = ..., **kwargs) -> str: ...


def default_llm() -> SupportsComplete:
    """설정된 provider로 기본 LLM 클라이언트를 만든다.

    import 시점에 설정/자격 증명을 요구하지 않도록 함수 안에서 import 한다.
    """

    from app.core.llm import LLMClient

    return LLMClient()


def items_to_text(items: list) -> str:
    """Pydantic 모델 리스트를 프롬프트용 JSON 문자열로 직렬화한다."""

    if not items:
        return "없음"
    dumped = [m.model_dump(by_alias=True, mode="json") for m in items]
    return json.dumps(dumped, ensure_ascii=False, indent=2)


def user_memory_to_text(user_memory: UserMemory | None) -> str:
    """user memory를 프롬프트용 문자열로 직렬화한다."""

    if user_memory is None:
        return "정보 없음"
    return json.dumps(user_memory.model_dump(by_alias=True), ensure_ascii=False)


def build_infer_prompt(
    user_memory_text: str,
    data_text: str,
    *,
    date: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> str:
    """추론 요청(user prompt): 사용자 정보 + 분석 데이터.

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
        f"[사용자 정보]\n{user_memory_text}\n\n"
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
        raise ValueError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
    payload = json.loads(text[start : end + 1])
    return AgentEventResult.model_validate(payload)
