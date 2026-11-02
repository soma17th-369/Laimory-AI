"""기억한 장소 (#192, v4 세트).

User Memory `notablePlacePoints` 는 사용자가 전에 고른 장소와 그 좌표다. 다음 타임라인에서 두 가지로 쓴다.

1. **후보 보강**(`add_remembered_places`) — 체류와 이동 도착 지점에서 250m 안(250m 포함)의 기억 장소를 그
   지점의 `places` 뒤에 더하고 `placePoints` 에 기억한 좌표를 넣는다. 그날 역지오코딩 후보에 없던 이름도
   후보가 된다. 입력에 들어가므로 후보 풀, 결과 저장 직전의 후보 대조와 좌표가 따로 고치지 않아도 이 이름을
   본다. 더한 원소는 `PlacePoint.remembered` 로 표시하고 위치 뼈대는 그 이름을 보지 않는다 — 이름으로 같은
   장소인지 보는 판정이 바뀌면 산책·귀가·빈 시간이 달라진다.
2. **전에 고른 곳**(`PlaceMemory.visited`) — 후보 가운데 사용자가 전에 고른 이름. 좌표가 저장된 이름은 그
   지점 250m 안일 때만이다. 이름이 같은 다른 지점(다른 동네의 같은 상호)은 아니다. 좌표 없이 이름만 있는
   예전 이름(#181)은 이름이 같으면 해당한다.

250m 는 후보에 넣는 기준이지 그곳에 갔다는 확정이 아니다. 같은 건물의 여러 장소 가운데 어느 곳인지는
Timeline 이 맥락을 보고 정한다.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.schemas import GeoPlace, StayItem, TimelineDraftRequest
from app.schemas.common import PlacePoint
from app.schemas.user_memory import NotablePlacePoint, UserMemory
from app.services.location_metrics import SAME_PLACE_MAX_METERS, distance_between
from app.services.place_text import normalize_place_text

Point = tuple[float, float]


@dataclass(frozen=True)
class PlaceMemory:
    points: tuple[NotablePlacePoint, ...] = ()
    #: 좌표 없이 이름만 있는 예전 이름(정규화한 값).
    unlocated: frozenset[str] = frozenset()

    @classmethod
    def of(cls, memory: UserMemory | None) -> "PlaceMemory":
        if memory is None:
            return cls()
        located = {normalize_place_text(point.name) for point in memory.notable_place_points}
        return cls(
            tuple(memory.notable_place_points),
            frozenset(
                key
                for name in memory.notable_places
                if (key := normalize_place_text(name)) and key not in located
            ),
        )

    def near(self, point: Point) -> list[NotablePlacePoint]:
        """그 지점 250m 안(포함)의 기억 장소. 가까운 순이다."""

        hits = [
            (distance, known)
            for known in self.points
            if (distance := distance_between((known.latitude, known.longitude), point)) is not None
            and distance <= SAME_PLACE_MAX_METERS
        ]
        return [known for _, known in sorted(hits, key=lambda hit: hit[0])]

    def visited(self, names: Iterable[str], points: Iterable[Point]) -> list[str]:
        """`names` 가운데 사용자가 전에 고른 이름. 순서는 `names` 그대로다."""

        nearby = {normalize_place_text(known.name) for point in points for known in self.near(point)}
        return [
            name for name in names if (key := normalize_place_text(name)) in nearby or key in self.unlocated
        ]


def add_remembered_places(request: TimelineDraftRequest) -> None:
    """체류와 이동 도착 지점의 `places` 에 250m 안의 기억 장소를 더한다(in-place)."""

    memory = PlaceMemory.of(request.user_memory)
    if not memory.points:
        return
    spots: list[StayItem | GeoPlace] = [
        *request.stays,
        *(movement.end for movement in request.movements if movement.end is not None),
    ]
    for spot in spots:
        if spot.latitude is None or spot.longitude is None:
            continue
        known = {normalize_place_text(name) for name in (spot.place, *spot.places) if name}
        for remembered in memory.near((spot.latitude, spot.longitude)):
            key = normalize_place_text(remembered.name)
            if key in known:
                continue
            known.add(key)
            spot.places.append(remembered.name)
            spot.place_points.append(
                PlacePoint(
                    name=remembered.name,
                    latitude=remembered.latitude,
                    longitude=remembered.longitude,
                    remembered=True,
                )
            )


def added_from_memory(points: list[PlacePoint]) -> set[str]:
    """기억에서 더한 이름(정규화한 값). 위치 뼈대는 이 이름을 빼고 본다."""

    return {normalize_place_text(point.name) for point in points if point.remembered}
