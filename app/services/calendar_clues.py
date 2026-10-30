"""일정 단서 (#168, v4 세트).

v3 Calendar Agent(LLM)가 일정마다 candidate 를 쓰던 일을 코드가 한다. 일정은 사용자가 손으로
적어 둔 계획이라 제목·시간이 그대로 근거이고, LLM 이 다시 쓸 것이 없다. 코드는 일정을 읽기
좋게 정리만 하고, 무엇을 한 일정인지(eventType)와 문장은 Timeline v4 가 정한다.

규칙의 출처는 v3 Calendar 프롬프트이고, 대응은 계획의 「일정 규칙 이식표」(C1~C11)에 있다.

- **모든 일정이 단서가 된다.** 시각을 읽지 못한 일정도 시간 없는 단서로 남긴다(C1).
- 제목과 시간은 그대로 싣는다. 참석 여부는 판단하지 않는다(C2).
- `locationText` 의 장소명과 주소를 가른다(C4). `집(경기도 오산시 운암로 90)` 은 장소명
  `집`, 주소 `경기도 오산시 운암로 90` 이다.
- 종일·여러 날·window 로 잘림·timezone 기본값 적용을 표시한다(C5·C7·C8).
- 종일이거나 12시간 이상인 일정은 **배경 일정**이다(C6, 내 선택. v3 예시는 14시간이다).
  배경 일정은 그 시간 내내 한 가지를 했다는 뜻이 아니라 하루의 맥락이다.
- 메모(`description`)는 앞 200자만 싣는다(C11, 내 선택). 요약하거나 마스킹하지 않는다 —
  provider 에 나가는 범위는 v3 Calendar 호출과 같다. 사용자 문장의 마스킹은 Timeline 이 한다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from app.schemas import CalendarItem, TimelineDraftRequest
from app.services.narrative_place_guard import looks_like_address
from app.services.place_resolver import calendar_place_label
from app.services.validator import parse_datetime, resolve_timezone

#: 이 길이 이상이면 하루의 배경 일정이다(내 선택).
BACKGROUND_MIN_DURATION = timedelta(hours=12)

#: 메모를 이 글자까지만 싣는다(내 선택). 일정 메모에 회의록·링크가 통째로 붙어 오기도 한다.
MEMO_MAX_LENGTH = 200


@dataclass(frozen=True)
class CalendarClue:
    """일정 하나를 정리한 단서."""

    clue_id: str
    raw_id: str
    title: str
    memo: str | None
    #: window 로 자른 시간. 시각을 읽지 못하면 둘 다 None 이다.
    start: datetime | None
    end: datetime | None
    #: 자르기 전 원래 시간.
    original_start: datetime | None
    original_end: datetime | None
    all_day: bool
    multi_day: bool
    clipped: bool
    timezone_defaulted: bool
    place: str | None
    address: str | None
    background: bool

    def as_prompt_dict(self) -> dict[str, Any]:
        """프롬프트에 실을 형태. 값이 없거나 거짓인 표시는 뺀다(빈 값에 의미를 붙이지 않게)."""

        payload: dict[str, Any] = {"id": self.clue_id, "title": self.title}
        if self.start is not None and self.end is not None:
            payload["startAt"] = self.start.isoformat()
            payload["endAt"] = self.end.isoformat()
        else:
            payload["untimed"] = True
        for key, value in (
            ("memo", self.memo),
            ("place", self.place),
            ("address", self.address),
        ):
            if value:
                payload[key] = value
        for key, flag in (
            ("allDay", self.all_day),
            ("multiDay", self.multi_day),
            ("background", self.background),
            ("clippedToWindow", self.clipped),
            ("timezoneDefaulted", self.timezone_defaulted),
        ):
            if flag:
                payload[key] = True
        if self.clipped and self.original_start and self.original_end:
            payload["originalStartAt"] = self.original_start.isoformat()
            payload["originalEndAt"] = self.original_end.isoformat()
        return payload


def build_calendar_clues(request: TimelineDraftRequest) -> list[CalendarClue]:
    """요청의 모든 일정을 단서로 만든다. 시간순이고, 시각을 못 읽은 일정은 맨 뒤다."""

    tz = resolve_timezone(request.timezone)
    window_start = parse_datetime(request.window.start, tz) if request.window else None
    window_end = parse_datetime(request.window.end, tz) if request.window else None

    rows = [_row(item, tz, window_start, window_end) for item in request.calendars]
    rows.sort(key=lambda row: (row["original_start"] is None, row["original_start"] or 0))
    return [
        CalendarClue(clue_id=f"C{number}", **row) for number, row in enumerate(rows, start=1)
    ]


def _row(
    item: CalendarItem,
    tz: tzinfo,
    window_start: datetime | None,
    window_end: datetime | None,
) -> dict[str, Any]:
    original_start = parse_datetime(item.start_at, tz)
    original_end = parse_datetime(item.end_at, tz) if item.end_at else original_start
    if original_start is not None and (original_end is None or original_end < original_start):
        original_end = original_start

    start, end, clipped = original_start, original_end, False
    if start is not None and end is not None:
        if window_start is not None and start < window_start:
            start, clipped = window_start, True
        if window_end is not None and end > window_end:
            end, clipped = window_end, True
        if end < start:
            # window 밖에 통째로 있는 일정. 버리지 않고 가까운 경계 한 점으로 둔다(C1).
            boundary = window_start if window_start is not None and end < window_start else end
            start = end = boundary

    place, address = split_location_text(item.location_text)
    duration = (
        original_end - original_start
        if original_start is not None and original_end is not None
        else timedelta(0)
    )
    return dict(
        raw_id=str(item.raw_id),
        title=item.title.strip(),
        memo=_memo(item.description),
        start=start,
        end=end,
        original_start=original_start,
        original_end=original_end,
        all_day=item.all_day,
        multi_day=_multi_day(original_start, original_end),
        clipped=clipped,
        timezone_defaulted=not (_has_offset(item.start_at) and _has_offset(item.end_at)),
        place=place,
        address=address,
        background=item.all_day or duration >= BACKGROUND_MIN_DURATION,
    )


def split_location_text(location_text: str | None) -> tuple[str | None, str | None]:
    """`locationText` 를 장소명과 주소로 가른다(C4)."""

    text = (location_text or "").strip()
    if not text:
        return None, None
    if "(" in text and text.endswith(")"):
        label = calendar_place_label(text)
        inside = text[text.index("(") + 1 : -1].strip()
        if looks_like_address(inside):
            return label, inside
        return text, None
    if looks_like_address(text):
        return None, text
    return text, None


def _memo(description: str | None) -> str | None:
    text = (description or "").strip()
    if not text:
        return None
    return text[:MEMO_MAX_LENGTH]


def _multi_day(start: datetime | None, end: datetime | None) -> bool:
    """날짜를 넘는 일정인가. 다음 날 0시에 끝나는 종일 일정은 하루짜리다."""

    if start is None or end is None or end <= start:
        return False
    return (end - timedelta(microseconds=1)).date() > start.date()


def _has_offset(value: str | None) -> bool:
    """시각 문자열에 timezone offset 이 있는가. 없으면 기본 timezone 을 적용한 것이다(C8)."""

    if value is None:
        return True
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False
