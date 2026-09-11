"""프롬프트 세트(v1/v2/v3)의 완전성과 버전별 Agent 구조 계약 (#56, #114).

`load_prompt` 는 요청한 파일이 없으면 다른 버전으로 대체하지 않고 실패한다. 그래서
어떤 버전이든 그 버전을 쓰는 Agent 가 읽는 파일이 **모두** 있어야 기동한다. 파일 하나가
빠지면 `PROMPT_VERSION` 을 바꾸는 순간 import 시점에 죽는다.

review 단계는 v1 전용이다. v2 부터는 단일 `complete_structured` 호출로 간다(#56).
이 계약이 깨지면 `PROMPT_VERSION=v1` 롤백이 더 이상 v1 동작을 되돌리지 못한다.
"""

import json
import re
from pathlib import Path

import pytest

from app.agents.events.notification.agent import build_notification_payload
from app.schemas import AiEventCandidate, NotificationItem
from app.services.location_metrics import MovementMetric, MovementMode
from tests.fixtures.requests import fixture_raw_id

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: 각 Agent 디렉터리가 자기 프롬프트 세트에서 읽어야 하는 파일.
#: (agent 디렉터리, 버전별 필수 파일)
_REQUIRED_PROMPTS: dict[str, dict[str, set[str]]] = {
    "agents/timeline": {
        "v1": {"timeline.md"},
        "v2": {"timeline.md"},
        "v3": {"timeline.md"},
    },
    "agents/repair": {"v1": {"prompt.md"}, "v2": {"prompt.md"}, "v3": {"prompt.md"}},
    "agents/question": {
        "v1": {"question.md"},
        "v2": {"question.md"},
        "v3": {"question.md"},
    },
    # User Memory 갱신(#64)은 타임라인 파이프라인 밖이지만 `PROMPT_VERSION` 은 전역이라
    # 모든 세트가 있어야 한다. 없는 버전으로 기동하면 import 시점에 죽는다.
    "agents/user_memory": {
        "v1": {"prompt.md"},
        "v2": {"prompt.md"},
        "v3": {"prompt.md"},
    },
    "agents/events/calendar": {
        "v1": {"prompt.md"},
        "v2": {"prompt.md"},
        "v3": {"prompt.md"},
    },
    "agents/events/notification": {
        "v1": {"prompt.md"},
        "v2": {"prompt.md"},
        "v3": {"prompt.md"},
    },
    "agents/events/location": {
        # review.md 는 v1 전용이다.
        "v1": {"prompt.md", "review.md"},
        "v2": {"prompt.md"},
        "v3": {"prompt.md"},
    },
    "agents/events/sleep_activity": {
        "v1": {"prompt.md", "review.md"},
        "v2": {"prompt.md"},
        "v3": {"prompt.md"},
    },
    "agents/events/photo": {
        # 두 파일 다 버전 무관 필수다(#59). 모든 사진이 describe_vision_prompt.md 로
        # 가고, 이미지를 못 구한 사진만 describe_prompt.md 로 간다.
        "v1": {"prompt.md", "describe_prompt.md", "describe_vision_prompt.md"},
        "v2": {"prompt.md", "describe_prompt.md", "describe_vision_prompt.md"},
        "v3": {"prompt.md", "describe_prompt.md", "describe_vision_prompt.md"},
    },
}

_VERSIONS = ("v1", "v2", "v3")

#: review 단계 없이 단일 structured 호출로 가는 버전(#56).
_SINGLE_CALL_VERSIONS = ("v2", "v3")


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


@pytest.mark.parametrize("version", _SINGLE_CALL_VERSIONS)
@pytest.mark.parametrize(
    "agent_dir", ["agents/events/location", "agents/events/sleep_activity"]
)
def test_single_call_versions_have_no_review_prompt(agent_dir: str, version: str) -> None:
    """v2 이후 세트에 review.md 를 다시 넣으면 v1 전용 분기와 어긋난다."""

    review = APP_ROOT / agent_dir / "prompts" / version / "review.md"

    assert not review.exists(), (
        f"{agent_dir} 의 {version} 세트에 review.md 가 있습니다. "
        f"review 단계는 v1 전용이며 {version} 은 단일 structured 호출로 갑니다(#56)."
    )


# --- 결과 품질 계약 (#61) -----------------------------------------------
#
# 아래는 전부 **버전을 명시**해서 읽는다. `load_prompt` 는 버전을 생략하면
# `PROMPT_VERSION` 을 따르는데, 이 저장소의 `.env` 는 v1 이라 그대로 두면 이 프롬프트가
# 한 번도 검사되지 않는다. #61 은 v2 세트를 바꿨고 v3 은 v2 를 이어받으므로(#114)
# 두 세트 모두 이 계약을 지켜야 한다.

_QUALITY_VERSIONS = ("v2", "v3")


def _prompt(agent_dir: str, version: str, filename: str = "prompt.md") -> str:
    return (APP_ROOT / agent_dir / "prompts" / version / filename).read_text(
        encoding="utf-8"
    )


def _timeline(version: str) -> str:
    return _prompt("agents/timeline", version, "timeline.md")


@pytest.mark.parametrize("version", _QUALITY_VERSIONS)
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
def test_timeline_states_tone_and_length(version: str, marker: str, why: str) -> None:
    assert marker in _timeline(version), f"timeline {version} 에 '{marker}' 가 없습니다. {why}"


@pytest.mark.parametrize("version", _QUALITY_VERSIONS)
def test_timeline_puts_tone_rules_before_input_section(version: str) -> None:
    """말투 규정은 프롬프트 **앞쪽**에 둔다 (#61).

    앞 지시가 더 세게 작동하므로 「결과 문장의 말투」를 「입력 의미」보다 먼저 둔다.
    뒤쪽 「제목과 설명」은 규정이 아니라 예시 자리다 — 같은 문장을 두 번 쓰지 않는다.
    """

    text = _timeline(version)
    tone = text.index("## 결과 문장의 말투")
    inputs = text.index("## 입력 의미")
    examples = text.index("## 제목과 설명")

    assert tone < inputs < examples


@pytest.mark.parametrize("version", _QUALITY_VERSIONS)
def test_timeline_keeps_future_reservation_out_of_today(version: str) -> None:
    assert "대상 날짜와 다르면" in _timeline(version)


@pytest.mark.parametrize("version", _QUALITY_VERSIONS)
def test_notification_separates_posted_at_from_schedule_date(version: str) -> None:
    """알림 수신 시각과 알림이 말하는 일정 날짜는 다르다 (#61).

    이 구분이 없으면 내일 예약 알림이 오늘 window 안 시각에 붙어 검증을 통과한다.
    """

    text = _prompt("agents/events/notification", version)
    assert "`postedAt`은 알림을 받은 시각" in text
    assert "대상 날짜와 **다르면**" in text
    assert "예약 행위" in text


@pytest.mark.parametrize("version", _QUALITY_VERSIONS)
def test_repair_checks_tone_and_length(version: str) -> None:
    text = _prompt("agents/repair", version)
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


# --- v3 Event Agent 공통 출력 (#114) ---------------------------------------
#
# 다섯 Event Agent 는 같은 candidate 계약으로 Timeline 에 결과를 넘긴다. 출력 예시에
# Agent 마다 다른 키가 섞이면 모델은 그 키를 계약으로 알고 채운다. 예시의 키를 스키마와
# 묶어 두면 스키마에서 필드를 빼고 예시를 그대로 두는 일(또는 그 반대)이 여기서 걸린다.

_EVENT_AGENTS = ("calendar", "location", "notification", "photo", "sleep_activity")

#: 코드가 근거 입력에서 복사하는 candidate 필드(#72). Agent 는 쓰지 않는다.
_CODE_FILLED_CANDIDATE_FIELDS = {"places", "address"}


def _event_prompt(agent: str) -> str:
    return _prompt(f"agents/events/{agent}", "v3")


def _output_example(agent: str) -> dict:
    block = re.search(r"```json\n(.*?)```", _event_prompt(agent), re.S)
    assert block, f"{agent} v3 프롬프트에 JSON 출력 예시가 없습니다."
    return json.loads(block.group(1))


def _agent_written_candidate_fields() -> set[str]:
    schema_fields = {
        field.alias or name for name, field in AiEventCandidate.model_fields.items()
    }
    return schema_fields - _CODE_FILLED_CANDIDATE_FIELDS


@pytest.mark.parametrize("agent", _EVENT_AGENTS)
def test_v3_event_output_example_uses_exactly_the_agent_written_fields(agent: str) -> None:
    """단수 `place`·`context`·`evidenceSummary`·`semanticTags` 는 계약에 없다."""

    example = _output_example(agent)
    keys = set().union(*(candidate.keys() for candidate in example["candidates"]))

    assert keys == _agent_written_candidate_fields(), (
        f"{agent} v3 출력 예시의 candidate 키가 공통 계약과 다릅니다: {sorted(keys)}"
    )


def test_candidate_contract_has_no_duplicate_summary_fields() -> None:
    """근거 요약과 의미 태그는 description 과 같은 사실을 한 번 더 적는 자리였다(#114)."""

    fields = _agent_written_candidate_fields()

    assert "evidenceSummary" not in fields
    assert "semanticTags" not in fields
    assert "place" not in fields


@pytest.mark.parametrize("agent", ["photo", "calendar"])
def test_v3_puts_detail_in_description(agent: str) -> None:
    text = _event_prompt(agent)

    assert "evidenceSummary" not in text
    assert "semanticTags" not in text
    assert "의미 태그" not in text
    assert "자세한 설명" in text, "description 이 자세한 묘사를 맡는다고 적어야 합니다."


def test_v3_photo_does_not_ask_agent_to_fill_code_filled_places() -> None:
    text = _event_prompt("photo")

    assert "코드가 근거 입력에서 채우므로 출력하지 않습니다" in text


def test_v3_calendar_interprets_schedule_without_checking_attendance() -> None:
    text = _event_prompt("calendar")

    assert "참석 여부를 확인하려 하지 않습니다" in text
    assert "참석 가능성" not in text, "위치로 참석을 확인하라는 지시가 남아 있습니다."
    assert "실제 수행 여부" not in text
    assert "위치가 충돌" not in text


def test_v3_calendar_states_all_day_in_description() -> None:
    assert "하루 종일인 일정임을 드러냅니다" in _event_prompt("calendar")


def test_v3_location_keeps_only_origin_and_final_destination() -> None:
    text = _event_prompt("location")

    assert "출발지와 최종 도착지만" in text
    assert "주요 도착지" not in text
    assert "교통 거점 도착 후" not in text, "경유지를 설명하는 제목 예시가 남아 있습니다."


def test_v3_location_judges_walk_by_round_trip_and_duration() -> None:
    text = _event_prompt("location")

    assert "30분 이상" in text
    assert "`EXERCISE`" in text
    assert "`WALK`" in text


def test_v3_location_keeps_transport_out_of_sentences() -> None:
    text = _event_prompt("location")

    assert "이동수단은 `title`과 `description`에 쓰지 않습니다" in text
    assert "노선" not in text, "열차·버스·노선 명칭을 쓰라는 지시가 남아 있습니다."
    assert "transportConflict" not in text


def test_v3_location_names_every_movement_metric_key() -> None:
    """프롬프트가 설명하는 derivedMetrics 키가 코드가 싣는 키와 맞아야 한다.

    코드에서 키 이름을 바꾸고 프롬프트를 두면, 모델은 없는 키를 찾고 있는 키는 모른다.
    """

    metric = MovementMetric(
        raw_id="raw",
        start=None,
        end=None,
        duration_minutes=1.0,
        distance_meters=1.0,
        average_speed_kmh=1.0,
        mode=MovementMode.WALK,
        mode_conflict="어긋남",
    )
    text = _event_prompt("location")

    for key in metric.as_prompt_dict():
        assert f"`{key}`" in text, f"location v3 에 derivedMetrics 키 `{key}` 설명이 없습니다."


# --- v3 Notification 대화·결제·예약 (#116) ---------------------------------


def test_v3_notification_fixes_information_to_three_kinds() -> None:
    text = _event_prompt("notification")

    assert "**대화·결제·예약**" in text
    for kind in ("`CONVERSATION`", "`PAYMENT`", "`RESERVATION`"):
        assert kind in text, f"notification v3 에 {kind} 설명이 없습니다."


def test_v3_notification_drops_code_made_guidance() -> None:
    """코드가 알림을 읽기 전에 내리던 판단은 입력에서 사라졌다. 설명이 남으면 모델이 찾는다."""

    text = _event_prompt("notification")

    for removed in (
        "timelineUseGuidance",
        "contextOnly",
        "messengerAnalysis",
        "messengerInterpretation",
        "appDictionary",
        "appPolicy",
    ):
        assert removed not in text, f"notification v3 에 없어진 입력 `{removed}` 설명이 남아 있습니다."


def test_v3_notification_names_every_payload_key() -> None:
    """프롬프트가 설명하는 입력 키가 코드가 싣는 키와 맞아야 한다.

    코드에서 키 이름을 바꾸고 프롬프트를 두면, 모델은 없는 키를 찾고 있는 키는 모른다.
    """

    def item(label: str, app_name: str, title: str) -> NotificationItem:
        return NotificationItem(
            rawId=fixture_raw_id(label),
            postedAt="2026-06-20T09:00:00+09:00",
            appName=app_name,
            title=title,
            text="본문",
        )

    payload = build_notification_payload(
        [item("pay", "토스", "결제"), item("chat", "카카오톡", "김민수")]
    )
    conversation = payload["conversations"][0]
    keys = (
        set(payload)
        | set(payload["policies"][0])
        | set(payload["notifications"][0])
        | set(conversation)
        | set(conversation["messages"][0])
    )
    text = _event_prompt("notification")

    for key in sorted(keys):
        assert f"`{key}`" in text, f"notification v3 에 입력 키 `{key}` 설명이 없습니다."


def test_v3_notification_limits_conversations_and_states_missing_input() -> None:
    text = _event_prompt("notification")

    assert "하루 최대 3개" in text
    assert "대화 참여자 목록은 입력에 없습니다" in text
    assert "메시지를 보낸 사람" in text, "단체방 title 이 보낸 사람일 수 있다고 알려야 합니다."
