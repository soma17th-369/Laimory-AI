"""v4 세트의 데이터별 단서 계약 (#169).

v3 는 Event Agent 다섯이 같은 candidate 양식(eventType·제목·문장)으로 사건을 미리 정했다.
v4 는 데이터마다 그 데이터가 말하는 것만 담은 **단서**를 내고, 무엇이 사건인지는 Timeline v4
가 단서를 모아 판단한다. 위치·일정 단서는 코드가 만들고(`location_skeleton`·`calendar_clues`),
사진·알림 단서는 LLM 이 읽은 결과를 코드가 받아 만든다.

LLM 출력 계약(`*Set`)은 입력의 짧은 id(`P1`·`n3`)로 항목을 가리킨다. UUID 를 옮겨 적게 하지
않는 것은, 추론을 끈 작은 모델이 UUID 를 틀리게 옮긴 실측이 있어서다. 그 클래스들의 docstring
은 JSON schema 로 provider 에 나가므로 짧게 두고, 설계 이유는 이 주석에 적는다.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.schemas.common import CamelModel


class PhotoInterpretation(CamelModel):
    """사진 한 장에서 읽은, 사용자가 그때 한 일."""

    photo_id: str = Field(alias="id")
    activity: str = Field(description="사진 내용을 근거로 사용자가 한 일 한 문장")
    uncertainty: list[str] = Field(default_factory=list)


class PhotoInterpretationSet(CamelModel):
    """사진 해석 결과."""

    photos: list[PhotoInterpretation] = Field(default_factory=list)


class NotificationClueKind(StrEnum):
    """알림이 전하는 내용의 종류. 앱이 아니라 내용으로 정한다(#135)."""

    CONVERSATION = "CONVERSATION"
    NEWS = "NEWS"
    PAYMENT = "PAYMENT"
    RESERVATION = "RESERVATION"
    ACTION_RESULT = "ACTION_RESULT"
    PROMOTION = "PROMOTION"


class NotificationClueOutput(CamelModel):
    """같은 일을 가리키는 알림 묶음 하나."""

    kind: NotificationClueKind
    item_ids: list[str] = Field(alias="items", min_length=1)
    counterpart: str | None = Field(default=None, description="대화 상대·발신자·가맹점")
    summary: str = Field(description="민감정보를 가린 내용 요약")
    schedule_today: bool | None = Field(
        default=None,
        alias="scheduleToday",
        description="알림이 말하는 일정·예약 날짜가 대상 날짜인가. 일정이 없으면 null",
    )


class NotificationClueSet(CamelModel):
    """알림 단서 결과."""

    clues: list[NotificationClueOutput] = Field(default_factory=list)


class PhotoClue(CamelModel):
    """사진 한 장의 단서. 좌표는 코드 안에서만 쓰고 프롬프트에 싣지 않는다(#80)."""

    clue_id: str = Field(alias="id")
    raw_id: str = Field(alias="rawId")
    taken_at: datetime | None = Field(alias="takenAt")
    places: list[str] = Field(default_factory=list)
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    description: str | None = None
    activity: str | None = None
    uncertainty: list[str] = Field(default_factory=list)


class NotificationClue(CamelModel):
    """알림 묶음 하나의 단서.

    `kind` 가 비어 있으면 모델이 이 알림을 읽지 못한 것이다(응답에서 빠졌거나 호출이 실패).
    알림을 버리지 않으려고 남기되 내용은 싣지 않는다 — 원문에는 민감정보가 있을 수 있다.
    """

    clue_id: str = Field(alias="id")
    kind: NotificationClueKind | None = None
    raw_ids: list[str] = Field(alias="rawIds", min_length=1)
    app_names: list[str] = Field(default_factory=list, alias="appNames")
    counterpart: str | None = None
    summary: str | None = None
    schedule_today: bool | None = Field(default=None, alias="scheduleToday")
    first_at: datetime | None = Field(default=None, alias="firstAt")
    last_at: datetime | None = Field(default=None, alias="lastAt")
