"""Langfuse tracing 어댑터.

Timeline 실행의 trace/span 계층과 LLM generation을 Langfuse Python SDK v4로
내보낸다. **AI 실행 관측(agent 트리·모델 지연·토큰/비용)은 Langfuse가 전담한다**
(이슈 #47). FastAPI 요청·오류·외부 연동 같은 운영 로그는 stdout JSON → Filebeat →
Elasticsearch 로 따로 흐르며, 애플리케이션은 Elasticsearch를 직접 호출하지 않는다.

라이프로그 본문은 위치·건강·캘린더 정보를 포함할 수 있다. 따라서 기본 정책은
``NONE``이며, 입력/출력을 byte 길이와 해시로 치환한다. ``SANITIZED``를 명시한
환경에서도 기존 redaction을 거친 값만 SDK에 넘기고, export 직전 마스킹을 한 번 더
적용한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Lock
from typing import Any, Literal

from langfuse import Langfuse, propagate_attributes
from langfuse.types import (
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanPatch,
)

from app.core.config import settings
from app.core.logging import SERVICE_NAME, get_logger
from app.core.redaction import (
    ContentCapture,
    capture_external_content,
    capture_payload,
    redact_text,
    redact_value,
)

logger = get_logger(__name__)

ObservationType = Literal[
    "span",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "guardrail",
    "generation",
    "embedding",
]


@dataclass
class TokenUsageAccumulator:
    """현재 관측 범위 아래의 모든 LLM generation 토큰을 thread-safe하게 합산한다."""

    _buckets: dict[str, int] = field(default_factory=dict)
    _generation_count: int = 0
    _lock: Lock = field(default_factory=Lock)

    def add(self, usage_details: Mapping[str, int]) -> None:
        with self._lock:
            self._generation_count += 1
            for key, value in usage_details.items():
                if key == "total" or value < 0:
                    continue
                self._buckets[key] = self._buckets.get(key, 0) + value

    def summary(self) -> dict[str, Any]:
        with self._lock:
            buckets = dict(sorted(self._buckets.items()))
            generation_count = self._generation_count

        input_tokens = sum(
            value for key, value in buckets.items() if "input" in key
        )
        output_tokens = sum(
            value for key, value in buckets.items() if "output" in key
        )
        return {
            "generationCount": generation_count,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": sum(buckets.values()),
            "byType": buckets,
        }


_TOKEN_USAGE_STACK: ContextVar[tuple[TokenUsageAccumulator, ...]] = ContextVar(
    "langfuse_token_usage_stack",
    default=(),
)


@contextmanager
def token_usage_scope() -> Iterator[TokenUsageAccumulator]:
    """하위 generation 토큰을 현재 범위와 모든 상위 범위에 동시에 누적한다."""

    accumulator = TokenUsageAccumulator()
    token = _TOKEN_USAGE_STACK.set((*_TOKEN_USAGE_STACK.get(), accumulator))
    try:
        yield accumulator
    finally:
        _TOKEN_USAGE_STACK.reset(token)


def record_token_usage(usage_details: Mapping[str, int]) -> None:
    """활성화된 모든 Agent/task 누산기에 generation usage를 한 번 기록한다."""

    for accumulator in _TOKEN_USAGE_STACK.get():
        accumulator.add(usage_details)


@contextmanager
def _isolated_context_manager(
    manager: Any,
    *,
    operation: str,
) -> Iterator[Any | None]:
    """부가 관측 context manager의 시작·종료 실패를 주 처리에서 격리한다.

    애플리케이션 본문에서 발생한 예외는 절대 삼키거나 Langfuse 종료 예외로
    바꾸지 않는다. 관측 시작에 실패하면 본문은 no-op 관측으로 계속 실행하고,
    종료에 실패하면 로그만 남긴다.
    """

    try:
        value = manager.__enter__()
    except Exception:  # noqa: BLE001 - 관측 시작 실패는 주 처리와 격리한다.
        logger.exception("%s 시작 실패(격리)", operation)
        yield None
        return

    try:
        yield value
    except BaseException as app_exc:
        try:
            manager.__exit__(
                type(app_exc),
                app_exc,
                app_exc.__traceback__,
            )
        except Exception:  # noqa: BLE001 - 원래 애플리케이션 예외를 보존한다.
            logger.exception("%s 종료 실패(원래 예외 보존)", operation)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - 관측 종료 실패는 주 처리와 격리한다.
            logger.exception("%s 종료 실패(격리)", operation)


def _json_safe(value: Any) -> Any:
    """SDK/OTel에 넘길 값을 JSON 직렬화 가능한 형태로 고정한다."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _content_policy() -> ContentCapture:
    return ContentCapture(settings.langfuse_capture_policy)


def capture_langfuse_body(value: Any) -> Any:
    """observation 의 input/output 을 정책에 따라 캡처한다.

    사용자 데이터를 싣는 자리는 여기뿐이므로 ``NONE`` 에서는 진단 지표만 남기고 나머지를
    전부 접는다. 값을 ``{"input": ...}`` 같은 봉투로 감싸지 않는다 — 봉투 키 이름이
    콘텐츠 정책 판정에 걸려 payload 가 통째로 사라지던 원인이다(이슈 #48).
    """

    return capture_external_content(
        _json_safe(value),
        _content_policy(),
        max_bytes=settings.langfuse_max_payload_bytes,
    )


def capture_langfuse_metadata(value: Mapping[str, Any]) -> Any:
    """observation 의 metadata 를 캡처한다.

    metadata 는 호출부가 직접 고른 라벨(agent/tool/iteration/provider/stage…)이라 본문이
    아니다. 여기에 기본 차단을 적용하면 지금 보이는 라벨까지 사라지므로, 본문 키만
    골라 지우는 기존 denylist 를 그대로 쓴다.
    """

    captured = capture_payload(
        {"metadata": _json_safe(value)},
        _content_policy(),
        max_bytes=settings.langfuse_max_payload_bytes,
    )
    # 크기 제한에 걸리면 capture_payload가 전체 payload 요약을 반환한다.
    return captured.get("metadata", captured)


def _mask_attribute(value: Any) -> Any:
    """OTel attribute 한 값을 빠르게 마스킹한다."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return redact_text(value)
        redacted = redact_value(decoded)
        if redacted == decoded:
            return redact_text(value)
        return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))

    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            redact_text(item) if isinstance(item, str) else item for item in value
        ]
    return value


def _mask_otel_spans(
    *, params: MaskOtelSpansParams
) -> MaskOtelSpansResult | None:
    """Langfuse export 직전에 문자열 attribute의 Secret/PII 패턴을 재마스킹한다."""

    patches = {}
    for identifier, span in params.spans.items():
        replacements = {}
        for key, value in span.attributes.items():
            masked = _mask_attribute(value)
            if masked != value:
                replacements[key] = masked
        if replacements:
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)
    return MaskOtelSpansResult(span_patches=patches) if patches else None


@lru_cache
def get_langfuse_client() -> Langfuse | None:
    """설정된 Langfuse client를 지연 생성한다. 비활성/오설정은 no-op이다."""

    if not settings.langfuse_enabled:
        return None

    secret_key = settings.langfuse_secret_key.get_secret_value()
    if not settings.langfuse_public_key or not secret_key:
        logger.warning(
            "Langfuse tracing 비활성화: LANGFUSE_PUBLIC_KEY/SECRET_KEY가 필요합니다."
        )
        return None

    # OTel Resource 는 Langfuse client 를 만들 때 한 번 조립된다. service.name 을 주지
    # 않으면 `unknown_service` 로 남아 어느 서비스가 보낸 trace 인지 화면에서 알 수 없다.
    # 운영 로그의 `service` 필드와 같은 값을 써야 두 시스템에서 같은 서비스로 보인다.
    # 운영자가 값을 지정했으면 그대로 존중한다.
    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)

    try:
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=secret_key,
            base_url=settings.langfuse_base_url,
            environment=settings.app_env,
            release=settings.agent_version,
            sample_rate=settings.langfuse_sample_rate,
            mask_otel_spans=_mask_otel_spans,
        )
    except Exception:  # noqa: BLE001 - tracing 설정이 주 처리를 깨면 안 된다.
        logger.exception("Langfuse client 초기화 실패(격리)")
        return None


@contextmanager
def trace_observation(
    name: str,
    *,
    as_type: ObservationType = "span",
    input: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
    model: str | None = None,
    model_parameters: Mapping[str, Any] | None = None,
    trace_context: Mapping[str, str] | None = None,
) -> Iterator[Any | None]:
    """현재 OTel 컨텍스트 아래에 관측 하나를 열고, 비활성 시 no-op한다."""

    client = get_langfuse_client()
    if client is None:
        yield None
        return

    try:
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": as_type,
            "metadata": capture_langfuse_metadata(metadata or {}),
        }
        if input is not None:
            kwargs["input"] = capture_langfuse_body(input)
        if model is not None:
            kwargs["model"] = model
        if model_parameters:
            kwargs["model_parameters"] = _json_safe(model_parameters)
        if trace_context is not None:
            kwargs["trace_context"] = dict(trace_context)
        manager = client.start_as_current_observation(**kwargs)
    except Exception:  # noqa: BLE001 - 관측 준비 실패는 주 처리와 격리한다.
        logger.exception("Langfuse observation 준비 실패(격리): name=%s", name)
        yield None
        return

    with _isolated_context_manager(
        manager,
        operation=f"Langfuse observation({name})",
    ) as observation:
        yield observation


@contextmanager
def trace_timeline_task(
    task_id: str,
    *,
    daily_record_id: int,
    window_start: str,
    window_end: str,
) -> Iterator[Any | None]:
    """taskId에 결정적으로 연결되는 Timeline 최상위 span trace를 연다.

    실제 Agent Graph의 루트는 이 span 아래의 ``main-agent``다. task 실행 경계를
    agent로 표시하면 비즈니스 Agent가 하나 더 있는 것처럼 보이므로 일반 span으로
    유지한다.
    """

    client = get_langfuse_client()
    if client is None:
        yield None
        return

    try:
        trace_id = client.create_trace_id(seed=task_id)
    except Exception:  # noqa: BLE001 - trace ID 생성 실패도 관측만 비활성화한다.
        logger.exception("Langfuse trace ID 생성 실패(격리): taskId=%s", task_id)
        trace_id = None

    # 이름·태그·환경·버전은 evaluator/dashboard가 안정적으로 참조할 trace 속성이다.
    try:
        attributes_manager = propagate_attributes(
            trace_name="generate-timeline",
            tags=["timeline"],
            environment=settings.app_env,
            version=settings.agent_version,
            # taskId는 Timeline 실행의 전역 상관관계 키다. Langfuse의 전용
            # session 필드에도 넣어 Traces 화면에서 `session:<taskId>`로 바로
            # 검색하고, 같은 task의 재시도 observation도 함께 볼 수 있게 한다.
            session_id=task_id,
            metadata={"taskId": task_id, "dailyRecordId": daily_record_id},
        )
    except Exception:  # noqa: BLE001 - 속성 전파 실패는 trace 본문과 격리한다.
        logger.exception(
            "Langfuse trace 속성 준비 실패(격리): taskId=%s",
            task_id,
        )
        attributes_manager = None

    @contextmanager
    def _run_trace() -> Iterator[Any | None]:
        with trace_observation(
            "generate-timeline",
            as_type="span",
            input={
                "taskId": task_id,
                "dailyRecordId": daily_record_id,
                "window": {"start": window_start, "end": window_end},
            },
            metadata={"feature": "timeline"},
            trace_context={"trace_id": trace_id} if trace_id is not None else None,
        ) as observation:
            yield observation

    if attributes_manager is None:
        with _run_trace() as observation:
            yield observation
        return

    with _isolated_context_manager(
        attributes_manager,
        operation=f"Langfuse trace 속성 전파({task_id})",
    ):
        with _run_trace() as observation:
            yield observation


def update_observation(
    observation: Any | None,
    *,
    input: Any | None = None,
    output: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
    usage_details: Mapping[str, int] | None = None,
    level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
    status_message: str | None = None,
) -> None:
    """관측 결과를 안전하게 갱신한다. SDK 오류는 주 처리에서 격리한다."""

    if observation is None:
        return
    try:
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = capture_langfuse_body(input)
        if output is not None:
            kwargs["output"] = capture_langfuse_body(output)
        if metadata:
            kwargs["metadata"] = capture_langfuse_metadata(metadata)
        if usage_details:
            kwargs["usage_details"] = dict(usage_details)
        if level is not None:
            kwargs["level"] = level
        if status_message is not None:
            kwargs["status_message"] = status_message
        observation.update(**kwargs)
    except Exception:  # noqa: BLE001 - tracing은 부가 기능이다.
        logger.exception("Langfuse observation 갱신 실패(격리)")


def flush_langfuse() -> None:
    """현재까지의 Langfuse 이벤트를 강제 전송한다. 실패는 격리한다."""

    client = get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:  # noqa: BLE001 - tracing은 부가 기능이다.
        logger.exception("Langfuse flush 실패(격리)")
