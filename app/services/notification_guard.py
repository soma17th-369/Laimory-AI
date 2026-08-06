"""Notification 결과의 민감정보·관계명 검사 (#56 §5.4).

알림 원문은 마스킹되지 않은 채 Agent 에 들어간다. `NotificationItem` 은 수집이 준
`title`/`text` 를 그대로 갖고 있어서, JWT·인증 토큰·계좌번호가 섞여 있을 수 있다.
프롬프트가 "출력에는 마스킹된 의미만" 이라고 지시하지만 그것만 믿을 수는 없다 —
한 번 새면 최종 일기에 그대로 남는다.

관계명도 같다. 알림 `title` 은 보낸 사람이나 대화방 이름이지 관계가 아니다.
`엄마`, `팀장님` 같은 호칭은 User Memory 에 등록돼 있을 때만 쓸 수 있다. 근거 없이
관계를 붙이면 사용자가 읽는 일기에 없던 사람이 생긴다.

여기서는 **검출만** 한다. 문자열을 지우면 문장이 깨지고, 어떻게 바꿔 쓸지는 의미 판단이다.
Agent·Timeline warning 으로 알려 Timeline 과 Repair 가 다시 판단하게 한다.
"""

import re

from app.core.logging import get_logger, log_fields
from app.schemas import (
    AgentEventResult,
    AgentWarning,
    TimelineDraft,
    TimelineDraftRequest,
    TimelineWarning,
    TimelineWarningSeverity,
)

logger = get_logger(__name__)

_AGENT_NAME = "notification"

#: 최종 결과에 절대 남으면 안 되는 문자열 형태.
#:
#: User Memory 갱신(#64)도 같은 목록을 쓴다. 알림에서 걸러야 할 값과 프로필에 남으면
#: 안 되는 값은 같은 것들이고, 목록이 둘로 갈리면 한쪽만 늘어나도 아무도 모른다.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # JWT: base64url 세 토막. 인증 토큰이 그대로 옮겨 적힌 경우다.
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+")),
    # Bearer 토큰과 API 키 접두사.
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE)),
    ("API_KEY", re.compile(r"\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9]{16,}")),
    # 카드번호(구분자 포함 16자리)와 국내 휴대폰 번호.
    ("CARD", re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b")),
    ("PHONE", re.compile(r"\b01[016-9][ -]?\d{3,4}[ -]?\d{4}\b")),
    # 계좌번호로 보이는 긴 숫자열(하이픈 포함 10자리 이상).
    ("ACCOUNT", re.compile(r"\b\d{2,6}-\d{2,6}-\d{4,8}\b")),
)

#: User Memory 근거 없이 쓰면 안 되는 관계 호칭.
_RELATION_TERMS: tuple[str, ...] = (
    "엄마",
    "아빠",
    "어머니",
    "아버지",
    "누나",
    "언니",
    "형",
    "오빠",
    "동생",
    "할머니",
    "할아버지",
    "이모",
    "삼촌",
    "고모",
    "여자친구",
    "남자친구",
    "아내",
    "남편",
    "팀장님",
    "부장님",
    "교수님",
)


def verify_notification_result(
    result: AgentEventResult, request: TimelineDraftRequest
) -> AgentEventResult:
    """민감정보 노출과 근거 없는 관계명을 검사해 warning 을 덧붙인다."""

    if not result.candidates and not result.fragments:
        return result

    texts = _result_texts(result)
    leaked = sorted({label for label, text in _find_sensitive(texts)})
    invented = sorted(_find_unsupported_relations(texts, request))

    if leaked:
        result.warnings.append(
            AgentWarning(
                agent_name=_AGENT_NAME,
                message=(
                    f"결과 문장에 민감정보로 보이는 값이 남아 있습니다({', '.join(leaked)}). "
                    "원문 대신 의미만 남긴 표현으로 바꿔야 합니다."
                ),
            )
        )
    if invented:
        result.warnings.append(
            AgentWarning(
                agent_name=_AGENT_NAME,
                message=(
                    f"User Memory 에 근거가 없는 관계 호칭이 쓰였습니다"
                    f"({', '.join(invented)}). 입력에 있는 이름이나 대화방 이름을 씁니다."
                ),
            )
        )

    if leaked or invented:
        # 어떤 종류가 걸렸는지만 남긴다. 값 자체는 어디에도 기록하지 않는다.
        logger.debug(
            "Notification 결과 검증 지적",
            extra=log_fields(
                sensitiveKinds=leaked,
                unsupportedRelationCount=len(invented),
            ),
        )
    return result


def verify_notification_draft(
    draft: TimelineDraft, request: TimelineDraftRequest
) -> None:
    """최종 event에 남은 민감정보와 근거 없는 관계명을 warning으로 표시한다.

    Event Agent 결과가 깨끗해도 Timeline/Repair가 문장을 다시 조립하는 과정에서 값이
    노출될 수 있다. 최종 확정 패스마다 draft를 다시 검사해 Repair가
    ``PRIVACY_EXPOSURE``로 처리할 결정론적 근거를 제공한다.
    """

    texts = _draft_texts(draft)
    leaked = sorted({label for label, text in _find_sensitive(texts)})
    invented = sorted(_find_unsupported_relations(texts, request))

    if leaked:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-privacy-exposure-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.HIGH,
                message=(
                    "최종 event에 민감정보로 보이는 값이 남아 있습니다"
                    f"({', '.join(leaked)}). 원문 대신 의미만 남긴 표현으로 바꿔야 합니다."
                ),
            )
        )
    if invented:
        draft.warnings.append(
            TimelineWarning(
                warning_id=f"warning-unsupported-relation-{len(draft.warnings) + 1:03d}",
                severity=TimelineWarningSeverity.MEDIUM,
                message=(
                    "최종 event에 User Memory 근거가 없는 관계 호칭이 쓰였습니다"
                    f"({', '.join(invented)}). 입력에 있는 이름이나 대화방 이름을 씁니다."
                ),
            )
        )

    if leaked or invented:
        logger.debug(
            "최종 Notification 근거 검증 지적",
            extra=log_fields(
                sensitiveKinds=leaked,
                unsupportedRelationCount=len(invented),
            ),
        )


def _result_texts(result: AgentEventResult) -> list[str]:
    """검사 대상 문자열. 사용자에게 보이는 값만 모은다."""

    texts: list[str] = []
    for candidate in result.candidates:
        texts.extend([candidate.title, candidate.description])
        texts.extend(candidate.uncertainty)
        if candidate.evidence_summary:
            texts.append(candidate.evidence_summary)
        texts.extend(candidate.semantic_tags)
    texts.extend(fragment.summary for fragment in result.fragments)
    return [text for text in texts if text]


def _draft_texts(draft: TimelineDraft) -> list[str]:
    """최종 결과에서 사용자에게 보이거나 근거 설명으로 쓰이는 문자열."""

    texts: list[str] = []
    for event in draft.events:
        texts.extend([event.title, event.description])
        texts.extend([event.place_label or "", event.address or ""])
        texts.extend(event.tags)
        texts.extend(event.uncertainty)
        texts.extend(ref.reason or "" for ref in event.source_refs)
    for question in draft.questions:
        texts.extend([question.question, question.reason])
    return [text for text in texts if text]


def _find_sensitive(texts: list[str]) -> list[tuple[str, str]]:
    return [
        (label, text)
        for text in texts
        for label, pattern in SENSITIVE_PATTERNS
        if pattern.search(text)
    ]


def _find_unsupported_relations(
    texts: list[str], request: TimelineDraftRequest
) -> set[str]:
    """User Memory 가 뒷받침하지 않는 관계 호칭을 찾는다."""

    known = _user_memory_text(request)
    return {
        term
        for term in _RELATION_TERMS
        if term not in known and any(term in text for text in texts)
    }


def _user_memory_text(request: TimelineDraftRequest) -> str:
    """User Memory 를 한 덩어리 문자열로. 등록된 관계명을 찾기 위한 용도다."""

    memory = getattr(request, "user_memory", None)
    if memory is None:
        return ""
    return str(memory.model_dump(by_alias=True))
