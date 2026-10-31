"""v4 의 제목·문장 제안 (사용자 결정 2026-10-31).

"코드로 제목과 문장 건드리는 거 전부 timeline 과 repair 로 넘겨야한다." 코드가 문장을 고치면 일정·사진이
말하는 내용이 틀 문장에 묻혔다. 그래서 v4 에서 코드는 event 구조(존재·시간·근거)만 맡고 글은 쓰지 않는다.

글 규칙은 그대로 코드에 있다 — 다만 **사본에서만 돌린다.** 달라진 값을 `TEXT_CORRECTION` 으로 Repair 에
넘기고, 쓰는 것은 Repair 다. 규칙마다 판정을 따로 만들지 않고 고치던 함수를 그대로 쓰므로 규칙이 하나다.
장소 자리만 확정이 직접 맞춘다(사용자 결정) — 문장의 장소 이름을 `{place}` 칸으로 바꾸고
(`fill_place_slots(slots_only=True)`), 제목과 이동 글에서 장소 이름을 뺀다(`keep_place_names_out`).
"""

from collections.abc import Callable
from functools import partial
from typing import Any

from app.schemas import TimelineDraft, TimelineDraftRequest, TimelineEventDraft
from app.services.clue_bundles import TimelineClues
from app.services.meal_guard import word_long_stay_meals
from app.services.place_slots import fill_place_slots, fix_counterpart_particles, fix_title_places
from app.services.skeleton_guard import restore_place_only_text


def suggest_text_fixes(
    draft: TimelineDraft, request: TimelineDraftRequest, clues: TimelineClues
) -> list[tuple[TimelineEventDraft, dict[str, Any]]]:
    """규칙을 사본에 돌려 달라진 event 와 제안을 돌려준다. `draft` 는 바꾸지 않는다."""

    probe = draft.model_copy(deep=True)
    passes: tuple[tuple[str, Callable[[], object]], ...] = (
        ("근거가 위치뿐인 event 는 어디에 있었는지만 말한다", partial(restore_place_only_text, probe, request, clues)),
        ("이동은 기록의 경로, 장소가 없는 칸은 주소", partial(fill_place_slots, probe, request, clues=clues)),
        ("대화 상대 뒤 조사는 받침에 맞춘다", partial(fix_counterpart_particles, probe, request, clues=clues)),
        ("제목의 장소 자리는 그 event 의 지명", partial(fix_title_places, probe, request, clues)),
        ("식사 사진을 품은 긴 체류는 `점심을 먹고 시간을 보냈다` 꼴", partial(word_long_stay_meals, probe)),
    )
    rules: dict[int, list[str]] = {}
    for rule, apply in passes:
        before = [_text_of(event) for event in probe.events]
        apply()
        for position, event in enumerate(probe.events):
            if _text_of(event) != before[position]:
                rules.setdefault(position, []).append(rule)

    found = []
    for position, names in rules.items():
        event, fixed = draft.events[position], probe.events[position]
        suggestion: dict[str, Any] = {"rules": names}
        if fixed.event_type != event.event_type:
            suggestion["eventType"] = fixed.event_type.value
        if fixed.title != event.title:
            suggestion["title"] = fixed.title
        if fixed.description != event.description:
            suggestion["description"] = fixed.description
        found.append((event, suggestion))
    return found


def _text_of(event: TimelineEventDraft) -> tuple[object, str, str]:
    return event.event_type, event.title, event.description
