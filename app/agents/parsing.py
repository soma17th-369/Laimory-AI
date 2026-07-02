"""Event Agent 공용 순수 유틸.

**graph 는 여기 두지 않는다.** graph 가 필요한 agent 는 자기 폴더 안에서 직접
구성한다. 이 모듈은 모든 agent 가 공통으로 쓰는 부수 유틸(입력 직렬화, user
memory 직렬화, LLM 응답 파싱, 기본 LLM 접근)만 담는다.
"""

import json
from typing import Protocol

from app.schemas import AgentEventResult, UserMemory


class SupportsComplete(Protocol):
    """LLM 클라이언트 최소 인터페이스(테스트에서 fake 주입용)."""

    def complete(self, prompt: str, *, system: str | None = ..., **kwargs) -> str: ...


def default_llm() -> SupportsComplete:
    """설정된 provider 로 기본 LLM 클라이언트를 만든다(지연 import).

    import 시점에 설정/자격 증명을 요구하지 않도록 함수 안에서 import 한다.
    """

    from app.core.llm import LLMClient

    return LLMClient()


def items_to_text(items: list) -> str:
    """source item 모델 리스트를 프롬프트용 JSON 문자열로 직렬화한다."""

    if not items:
        return "없음"
    dumped = [m.model_dump(by_alias=True, mode="json") for m in items]
    return json.dumps(dumped, ensure_ascii=False, indent=2)


def user_memory_to_text(user_memory: UserMemory | None) -> str:
    """user memory 를 프롬프트용 문자열로 직렬화한다."""

    if user_memory is None:
        return "정보 없음"
    return json.dumps(user_memory.model_dump(by_alias=True), ensure_ascii=False)


def build_infer_prompt(user_memory_text: str, data_text: str) -> str:
    """추론 요청(user 프롬프트): 사용자 정보 + 데이터."""

    return (
        f"[사용자 정보]\n{user_memory_text}\n\n"
        f"[분석할 데이터]\n{data_text}\n\n"
        "위 데이터에서 있었을 법한 event 후보와 fragment 를 추론해 "
        "지정된 JSON 형식으로 출력하세요."
    )


def parse_agent_result(text: str) -> AgentEventResult:
    """LLM JSON 응답을 `AgentEventResult` 로 파싱·검증한다.

    코드펜스나 앞뒤 잡텍스트가 있어도 첫 `{` ~ 마지막 `}` 구간을 잘라 파싱한다.
    스키마 검증(pydantic)까지 통과해야 하므로 잘못된 enum/필드는 여기서 걸린다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
    payload = json.loads(text[start : end + 1])
    return AgentEventResult.model_validate(payload)
