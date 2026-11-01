"""방문 장소 기억의 실제 LLM 사례 (#181).

    LAIMORY_LIVE_LLM=1 PROMPT_VERSION=v4 pytest tests/integration/test_place_memory_live.py \
        -m live_llm -p no:cacheprovider

프롬프트를 고치면 이 사례를 다시 돌린다. 모델의 판단이라 실행마다 조금씩 다르므로, 여기에는
실측에서 흔들리지 않은 것만 단언한다. 입력의 이름은 모두 지어낸 값이다 — 운영 trace 는 커밋하지 않는다.
"""

import asyncio
import os
from datetime import datetime, timezone

import pytest

from app.agents.main import run_main_agent
from app.agents.question import QuestionAgent
from app.agents.user_memory import UserMemoryAgent
from app.core.config import settings
from app.schemas.user_memory import UserMemory
from app.schemas.user_memory_update import DailyTimeline
from app.services.user_memory_limits import build_daily_timeline_digest
from app.services.user_memory_repair import build_user_memory
from tests.fixtures.requests import make_request, stay_item
from tests.fixtures.user_memory import daily_timeline, daily_timeline_event

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_llm,
    pytest.mark.skipif(os.getenv("LAIMORY_LIVE_LLM") != "1", reason="LAIMORY_LIVE_LLM=1 이 필요합니다"),
    pytest.mark.skipif(settings.prompt_version != "v4", reason="PROMPT_VERSION=v4 가 필요합니다"),
]

_PROFILE = UserMemory(
    basic_profile="망원동 거주 직장인으로 보임. OO타워 근무.",
    routines="평일 아침 출근, 저녁까지 회사 근무.",
)


def _update(existing: UserMemory, *events: dict, date: str = "2026-11-02") -> UserMemory:
    day = DailyTimeline.model_validate(daily_timeline(record_date=date, events=list(events)))
    digest = build_daily_timeline_digest([day])
    outcome = build_user_memory(
        UserMemoryAgent(), existing, digest, updated_at=datetime(2026, 11, 8, tzinfo=timezone.utc)
    )
    return outcome.memory


def _meal(title: str, subtitle: str | None, memo: str | None = None) -> dict:
    return daily_timeline_event(
        title=title,
        subtitle=subtitle,
        memo=memo,
        start_at="2026-11-02T12:10:00+09:00",
        end_at="2026-11-02T13:00:00+09:00",
    )


def test_a_place_only_the_subtitle_names_is_kept():
    memory = _update(_PROFILE, _meal("친구와 파스타", "OO파스타 성수점에서 친구와 파스타를 먹었다."))

    assert memory.notable_places == ["OO파스타 성수점"]


def test_a_place_already_in_the_list_is_not_added_again():
    existing = _PROFILE.model_copy(update={"notable_places": ["망원시장", "OO타워"]})

    memory = _update(existing, _meal("친구와 칼국수", "망원시장에서 친구와 칼국수를 먹었다."))

    assert memory.notable_places.count("망원시장") == 1


def test_a_place_the_profile_already_knows_is_still_kept():
    """프로필에 회사가 있어도 이름은 리스트에 들어간다. 글자 칸일 때는 여기서 빠졌다."""

    work = daily_timeline_event(
        event_type="WORK",
        title="도화동에서 업무",
        subtitle="OO타워에서 회의와 문서 작업을 했다.",
        start_at="2026-11-02T10:10:00+09:00",
        end_at="2026-11-02T18:00:00+09:00",
    )

    memory = _update(_PROFILE, work)

    assert memory.notable_places == ["OO타워"]


class _NoQuestions(QuestionAgent):
    def generate(self, request, draft):
        return draft


def test_a_remembered_place_outside_the_candidates_never_appears():
    request = make_request(
        stays=[
            stay_item(
                "afternoon",
                start="2026-06-20T13:00:00+09:00",
                end="2026-06-20T15:00:00+09:00",
                places=["OO문구", "OO서점", "OO약국"],
                address="서울특별시 마포구 서교동 1",
            )
        ],
        user_memory=UserMemory(notable_places=["가나다시장"]),
    )

    draft = asyncio.run(run_main_agent(request, question_agent=_NoQuestions()))

    assert draft.events
    assert all("가나다시장" not in event.place_candidates for event in draft.events)
