"""테스트용 가짜 LLM 클라이언트.

실제 LLM 을 호출하지 않고, 미리 정한 응답을 순서대로 돌려준다. 각 호출의
프롬프트/시스템 프롬프트를 기록해 검증에 쓴다.
"""

import json
from types import SimpleNamespace

from app.core.structured import run_structured
from tests.fixtures.requests import fixture_raw_id


class FakeLLM:
    """`complete()` 를 흉내내는 가짜 LLM.

    Args:
        responses: 호출 순서대로 돌려줄 응답 문자열들. 호출이 응답 수를 넘으면
            마지막 응답을 반복한다.
    """

    def __init__(self, responses: list[str | Exception]) -> None:
        assert responses, "responses 는 최소 1개 필요합니다."
        self._responses = list(responses)
        self.calls: list[SimpleNamespace] = []

    def _next(self) -> str:
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        response = self._responses[idx]
        if isinstance(response, Exception):
            raise response
        return response

    def complete(self, prompt: str, *, system: str | None = None, **kwargs) -> str:
        self.calls.append(
            SimpleNamespace(prompt=prompt, system=system, images=None, kwargs=kwargs)
        )
        return self._next()

    def complete_with_images(
        self, prompt: str, images, *, system: str | None = None, **kwargs
    ) -> str:
        self.calls.append(
            SimpleNamespace(
                prompt=prompt, system=system, images=list(images), kwargs=kwargs
            )
        )
        return self._next()

    def complete_json(
        self,
        prompt: str,
        schema=None,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        **kwargs,
    ) -> str:
        """canned 응답을 그대로 돌려준다(실 provider 의 JSON 모드 자리).

        가짜 LLM 은 형태를 강제할 실 provider 가 없으므로 `complete` 와 같이 미리
        정한 응답을 순서대로 돌려준다. 호출도 함께 기록한다.
        """

        self.calls.append(
            SimpleNamespace(prompt=prompt, system=system, images=None, kwargs=kwargs)
        )
        return self._next()

    def complete_structured(
        self,
        prompt: str,
        schema,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_repairs: int = 1,
        **kwargs,
    ):
        """실제 `LLMClient.complete_structured` 와 같은 검증·재시도 경로를 태운다.

        canned 응답(JSON 문자열)을 `complete_json` 으로 돌려주고, 공통 `run_structured`
        가 스키마 검증과 교정 재시도를 처리한다. 실 클라이언트와 동일하게 동작하도록
        `run_structured` 를 그대로 재사용한다.
        """

        return run_structured(
            self.complete_json,
            prompt,
            schema,
            system=system,
            temperature=temperature,
            max_repairs=max_repairs,
        )


def result_json(candidates: list | None = None, fragments: list | None = None) -> str:
    """AgentEventResult 형태의 JSON 문자열을 만든다."""

    return json.dumps(
        {"candidates": candidates or [], "fragments": fragments or []},
        ensure_ascii=False,
    )


def candidate(
    event_type,
    source_refs,
    *,
    start="2026-06-20T09:00:00+09:00",
    end="2026-06-20T10:00:00+09:00",
    confidence=0.8,
    inference_level="EVIDENCE_BASED",
    uncertainty=("불확실",),
) -> dict:
    return {
        "eventType": event_type,
        "timeRange": {"startTime": start, "endTime": end},
        "title": "제목",
        "description": "설명",
        "sourceRefs": [
            {"sourceType": st, "rawId": fixture_raw_id(raw_id)}
            for st, raw_id in source_refs
        ],
        "confidence": confidence,
        "inferenceLevel": inference_level,
        "uncertainty": list(uncertainty),
    }


def fragment(source_type, raw_id, summary="요약", *, time_range=None) -> dict:
    payload = {
        "sourceType": source_type,
        "rawId": fixture_raw_id(raw_id),
        "summary": summary,
    }
    if time_range is not None:
        payload["timeRange"] = time_range
    return payload
