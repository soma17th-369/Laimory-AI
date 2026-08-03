"""프롬프트 세트(v1/v2)의 완전성과 버전별 Agent 구조 계약 (#56).

`load_prompt` 는 요청한 파일이 없으면 다른 버전으로 대체하지 않고 실패한다. 그래서
어떤 버전이든 그 버전을 쓰는 Agent 가 읽는 파일이 **모두** 있어야 기동한다. 파일 하나가
빠지면 `PROMPT_VERSION` 을 바꾸는 순간 import 시점에 죽는다.

review 단계는 v1 전용이다. v2 는 단일 `complete_structured` 호출로 간다(#56).
이 계약이 깨지면 `PROMPT_VERSION=v1` 롤백이 더 이상 v1 동작을 되돌리지 못한다.
"""

from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: 각 Agent 디렉터리가 자기 프롬프트 세트에서 읽어야 하는 파일.
#: (agent 디렉터리, 버전별 필수 파일)
_REQUIRED_PROMPTS: dict[str, dict[str, set[str]]] = {
    "agents/timeline": {"v1": {"timeline.md"}, "v2": {"timeline.md"}},
    "agents/repair": {"v1": {"prompt.md"}, "v2": {"prompt.md"}},
    "agents/events/calendar": {"v1": {"prompt.md"}, "v2": {"prompt.md"}},
    "agents/events/notification": {"v1": {"prompt.md"}, "v2": {"prompt.md"}},
    "agents/events/location": {
        # review.md 는 v1 전용이다.
        "v1": {"prompt.md", "review.md"},
        "v2": {"prompt.md"},
    },
    "agents/events/sleep_activity": {
        "v1": {"prompt.md", "review.md"},
        "v2": {"prompt.md"},
    },
    "agents/events/photo": {
        # 두 파일 다 버전 무관 필수다(#59). 모든 사진이 describe_vision_prompt.md 로
        # 가고, 이미지를 못 구한 사진만 describe_prompt.md 로 간다.
        "v1": {"prompt.md", "describe_prompt.md", "describe_vision_prompt.md"},
        "v2": {"prompt.md", "describe_prompt.md", "describe_vision_prompt.md"},
    },
}

_VERSIONS = ("v1", "v2")


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("agent_dir", sorted(_REQUIRED_PROMPTS))
def test_prompt_set_has_every_file_the_agent_loads(agent_dir: str, version: str) -> None:
    prompt_dir = APP_ROOT / agent_dir / "prompts" / version
    missing = {
        name
        for name in _REQUIRED_PROMPTS[agent_dir][version]
        if not (prompt_dir / name).is_file()
    }

    assert not missing, (
        f"{agent_dir} 의 {version} 세트에 {sorted(missing)} 이(가) 없습니다. "
        f"load_prompt 는 다른 버전으로 대체하지 않으므로 PROMPT_VERSION={version} 기동이 실패합니다."
    )


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("agent_dir", sorted(_REQUIRED_PROMPTS))
def test_prompt_files_are_not_empty(agent_dir: str, version: str) -> None:
    prompt_dir = APP_ROOT / agent_dir / "prompts" / version

    for name in sorted(_REQUIRED_PROMPTS[agent_dir][version]):
        path = prompt_dir / name
        if not path.is_file():
            continue
        assert path.read_text(encoding="utf-8").strip(), f"{path} 가 비어 있습니다."


@pytest.mark.parametrize("version", _VERSIONS)
def test_every_version_has_metadata_describe_prompt(version: str) -> None:
    """모든 버전이 `describe_prompt.md` 를 가져야 한다(#59).

    이미지를 못 구한 사진의 fallback 이 버전과 무관하게 이 파일을 읽는다. 없으면
    `load_prompt` 가 import 시점에 터져 그 버전으로 기동조차 못 한다.
    """

    path = APP_ROOT / "agents/events/photo/prompts" / version / "describe_prompt.md"

    assert path.is_file(), (
        f"photo {version} 세트에 describe_prompt.md 가 없습니다. "
        "vision 실패 시 채울 프롬프트라 버전마다 있어야 합니다(#59)."
    )


@pytest.mark.parametrize(
    "agent_dir", ["agents/events/location", "agents/events/sleep_activity"]
)
def test_v2_has_no_review_prompt(agent_dir: str) -> None:
    """v2 에 review.md 를 다시 넣으면 v1 전용 분기와 어긋난다."""

    review = APP_ROOT / agent_dir / "prompts" / "v2" / "review.md"

    assert not review.exists(), (
        f"{agent_dir} 의 v2 세트에 review.md 가 있습니다. "
        "review 단계는 v1 전용이며 v2 는 단일 structured 호출로 갑니다(#56)."
    )


# --- v2 결과 품질 계약 (#61) -------------------------------------------
#
# 아래는 전부 **v2 를 명시**해서 읽는다. `load_prompt` 는 버전을 생략하면
# `PROMPT_VERSION` 을 따르는데, 이 저장소의 `.env` 는 v1 이라 그대로 두면 v2 프롬프트가
# 한 번도 검사되지 않는다. #61 에서 바꾼 것은 v2 세트뿐이므로 버전을 고정한다.

_TIMELINE_V2 = APP_ROOT / "agents/timeline/prompts/v2/timeline.md"
_REPAIR_V2 = APP_ROOT / "agents/repair/prompts/v2/prompt.md"
_NOTIFICATION_V2 = APP_ROOT / "agents/events/notification/prompts/v2/prompt.md"


def _text(path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("1인칭", "말투가 1인칭임을 명시해야 합니다."),
        ("해요체", "종결어미를 해요체로 고정해야 합니다."),
        ("100자", "description 길이 기준이 있어야 합니다."),
        ("30자", "title 길이 기준이 있어야 합니다."),
        ("듯해요", "헤지 표현을 금지 대상으로 보여 줘야 합니다."),
        ("원본 수치", "분 단위 시각·걸음 수 같은 raw 값 금지가 있어야 합니다."),
    ],
)
def test_timeline_v2_states_tone_and_length(marker: str, why: str) -> None:
    assert marker in _text(_TIMELINE_V2), f"timeline v2 에 '{marker}' 가 없습니다. {why}"


def test_timeline_v2_puts_tone_rules_before_input_section() -> None:
    """말투 규정은 프롬프트 **앞쪽**에 둔다 (#61).

    앞 지시가 더 세게 작동하므로 「결과 문장의 말투」를 「입력 의미」보다 먼저 둔다.
    뒤쪽 「제목과 설명」은 규정이 아니라 예시 자리다 — 같은 문장을 두 번 쓰지 않는다.
    """

    text = _text(_TIMELINE_V2)
    tone = text.index("## 결과 문장의 말투")
    inputs = text.index("## 입력 의미")
    examples = text.index("## 제목과 설명")

    assert tone < inputs < examples


def test_timeline_v2_keeps_future_reservation_out_of_today() -> None:
    assert "대상 날짜와 다르면" in _text(_TIMELINE_V2)


def test_notification_v2_separates_posted_at_from_schedule_date() -> None:
    """알림 수신 시각과 알림이 말하는 일정 날짜는 다르다 (#61).

    이 구분이 없으면 내일 예약 알림이 오늘 window 안 시각에 붙어 검증을 통과한다.
    """

    text = _text(_NOTIFICATION_V2)
    assert "`postedAt`은 알림을 받은 시각" in text
    assert "대상 날짜와 **다르면**" in text
    assert "예약 행위" in text


def test_repair_v2_checks_tone_and_length() -> None:
    text = _text(_REPAIR_V2)
    assert "VERBOSE_NARRATION" in text, "문장 길이 초과를 잡는 문제 유형이 있어야 합니다."
    assert "해요체" in text, "Repair 가 목표 말투를 알아야 다시 쓸 수 있습니다."
    assert "헤지" in text, "추정 표현을 문제로 잡아야 합니다."


@pytest.mark.parametrize(
    "agent_dir", ["agents/events/location", "agents/events/sleep_activity"]
)
def test_review_prompt_keeps_draft_placeholder(agent_dir: str) -> None:
    """v1 review 프롬프트는 `{{DRAFT}}` 를 치환해 쓴다. 자리표시자가 없으면 초안이 사라진다."""

    review = APP_ROOT / agent_dir / "prompts" / "v1" / "review.md"

    assert "{{DRAFT}}" in review.read_text(encoding="utf-8")
