"""프롬프트 v2.2.0 재설계 계약 (#67).

이 파일은 두 가지를 지킨다.

    1. **부재 테스트** — 이번에 제거하기로 한 충돌 문구가 활성 프롬프트와 동적
       지시문에 남아 있지 않은지 본다. 새 규칙을 추가하는 것만으로는 예전 문구가
       계속 반대 방향을 지시한다.
    2. **정본 위치** — 결정론 코드가 보장하는 정책을 프롬프트가 여러 곳에서 반복하지
       않는지 본다.

동결본(`*_v2.1.0.md`)은 검사하지 않는다. 롤백 기준선이라 옛 문구가 그대로 있는 것이
정상이다.
"""

from pathlib import Path

import pytest

from app.agents.timeline.timeline_agent import build_timeline_prompt
from tests.fixtures.requests import make_request

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

_TIMELINE_V2 = APP_ROOT / "agents/timeline/prompts/v2/timeline.md"
_REPAIR_V2 = APP_ROOT / "agents/repair/prompts/v2/prompt.md"
_LOCATION_V2 = APP_ROOT / "agents/events/location/prompts/v2/prompt.md"

#: 재설계 기준선. 이 합계를 넘기면 규칙을 누적하고 있다는 뜻이다.
_BASELINE_TOTAL_CHARS = 23752


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- 부재 테스트 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "why"),
    [
        ("주요 경유", "장거리 이동은 출발지→최종 도착지로 쓴다. 경유 나열을 유도하면 안 된다."),
        ("직접 기록된 수면", "수면은 더 이상 지속시간 예외의 근거가 아니다."),
        ("유효한 수면·기상 candidate", "수면 candidate 사용 지시는 비노출 정책과 충돌한다."),
    ],
)
def test_timeline_v2_dropped_conflicting_phrases(phrase: str, why: str) -> None:
    assert phrase not in _text(_TIMELINE_V2), f"timeline v2 에 '{phrase}' 가 남아 있습니다. {why}"


def test_timeline_v2_forbids_earliest_to_latest_expansion() -> None:
    """최초~최후 확장은 비연속 근거를 연속 구간으로 만든다.

    단어 부재로는 검사할 수 없다 — 금지 문장 자체가 그 표현을 담기 때문이다.
    지시가 **금지 방향**인지를 본다.
    """

    text = _text(_TIMELINE_V2)
    assert "가장 이른 시각부터 가장 늦은 시각까지 구간을 늘리지 마세요" in text
    assert "비연속 메시지의 최초~최후는 연속 대화 구간이 아닙니다" in text


def test_timeline_v2_does_not_merge_on_time_or_place_alone() -> None:
    """`시간이 겹치거나 ... 같은 장소` 만으로 병합하라는 지시가 없어야 한다."""

    text = _text(_TIMELINE_V2)
    assert "시간 겹침만으로" in text, "시간 겹침만의 병합을 금지하는 문장이 있어야 합니다."
    assert "장소를 둘 다 모른다는 이유로 병합하지 않습니다" in text


def test_timeline_v2_forbids_sleep_events() -> None:
    text = _text(_TIMELINE_V2)
    assert "`SLEEP`·`WAKE_UP` event를 만들지 않습니다" in text


def test_timeline_v2_states_place_priority() -> None:
    text = _text(_TIMELINE_V2)
    place_section = text[text.index("## 장소") :]
    for marker in ("생활 장소명", "상호", "주소"):
        assert marker in place_section, f"장소 우선순위에 '{marker}' 가 없습니다."


def test_repair_v2_is_a_checklist_not_a_generation_copy() -> None:
    """Repair 는 생성 규칙 복제본이 아니라 문제 탐지 체크리스트다."""

    text = _text(_REPAIR_V2)
    assert "코드가 이미 보장하는 것" in text
    assert "MEANING_LOST" in text, "의미 유실을 별도 문제 유형으로 잡아야 합니다."
    assert "CALENDAR_EVIDENCE_MISSING" in text, "캘린더 보강 누락을 잡아야 합니다."
    assert "최소한으로 고칩니다" in text


def test_repair_v2_does_not_reapply_sleep_boundary() -> None:
    """수면 경계 재적용 도구는 사라졌다. 프롬프트가 부르게 두면 안 된다."""

    assert "enforce_sleep_boundary" not in _text(_REPAIR_V2)


def test_repair_v2_forbids_no_op_and_repeated_fixes() -> None:
    text = _text(_REPAIR_V2)
    assert "아무것도 바꾸지 않는 수정을 계획하지 마세요" in text
    assert "같은 문제를 같은 방식으로 다시 잡지 마세요" in text


# --- 동적 지시문이 시스템 정책을 다시 주입하지 않는다 -------------------------


@pytest.mark.parametrize(
    "phrase",
    ["overlapping time", "earliest", "latest endTime", "same place"],
)
def test_dynamic_user_prompt_does_not_reinject_merge_policy(phrase: str) -> None:
    """`build_timeline_prompt` 는 데이터와 짧은 요청만 담는다 (#67).

    예전 동적 지시문은 시스템 프롬프트가 금지한 `시간 겹침 또는 같은 장소` 병합과
    `최초~최후 확장` 을 영어로 다시, 더 강하게 지시했다.
    """

    prompt = build_timeline_prompt(make_request(), "없음", "없음", "없음")

    assert phrase not in prompt, f"동적 user prompt 에 '{phrase}' 가 남아 있습니다."


def test_dynamic_user_prompt_still_carries_the_data() -> None:
    request = make_request()
    prompt = build_timeline_prompt(request, "메모리", "후보", "단서")

    assert request.date in prompt
    assert request.window.start in prompt
    assert request.window.end in prompt
    assert "메모리" in prompt and "후보" in prompt and "단서" in prompt


# --- 크기 -------------------------------------------------------------------


def test_active_prompt_total_does_not_grow() -> None:
    """정적 본문 합계가 재설계 기준선을 넘지 않는다 (#67).

    규칙을 문단으로 계속 덧붙이면 입력 토큰과 충돌 가능성이 함께 는다. 개별 프롬프트가
    커져야 한다면 다른 곳의 중복을 먼저 지운다.
    """

    total = sum(len(_text(path)) for path in (_TIMELINE_V2, _REPAIR_V2, _LOCATION_V2))

    assert total <= _BASELINE_TOTAL_CHARS, (
        f"활성 프롬프트 합계가 {total}자로 기준선 {_BASELINE_TOTAL_CHARS}자를 넘었습니다."
    )
