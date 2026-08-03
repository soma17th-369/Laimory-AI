"""Location Agent 결과 검증 (#56 §4.4 검증 코드).

Location Agent 는 v2 에서 "여러 이동을 하나의 상위 여정으로 묶고, 데이터가 끊긴 구간의
불확실성을 남긴다" 는 큰 일을 맡았다. 그 일은 확률적이라 조용히 안 될 수 있다 — 장거리
이동 raw 가 여러 건인데 상위 여정 candidate 가 하나도 없거나, 위치 기록이 끊겼는데 결과
어디에도 그 사실이 없거나.

이 모듈은 그 실패를 **입력과 결과를 대조해** 찾아낸다. 고치지는 않는다. 여정을 어떻게
묶을지는 의미 판단이고, 코드가 대신 묶으면 근거 없는 event 를 만들게 된다. 여기서는
Agent 결과에 warning 을 붙여 Timeline 과 Repair 가 그 사실을 알고 판단하게 한다.

`AgentEventResult.warnings` 로 나가므로 Timeline Agent 의 draft warning 으로 이어지고,
Repair Agent 는 `[근거 원본]` 과 함께 이 warning 을 본다.
"""

from dataclasses import dataclass

from app.core.logging import get_logger, log_fields
from app.schemas import AgentEventResult, AgentWarning, TimelineDraftRequest
from app.services.location_metrics import LocationMetrics, build_location_metrics
from app.services.source_lookup import raw_id_of

logger = get_logger(__name__)

#: 이 거리를 넘는 이동은 "장거리" 로 본다. 지역을 넘나드는 이동을 잡기 위한 값이라
#: 시내 이동(수 km)과 확실히 갈라지는 선으로 둔다.
LONG_DISTANCE_METERS = 20_000.0

#: 장거리 이동이 이 건수 이상이면 하나의 여정으로 묶였어야 한다.
LONG_DISTANCE_MIN_COUNT = 2

#: 상위 여정 candidate 로 인정하는 최소 근거 수. 여러 이동을 묶었다면 그만큼 참조가 남는다.
JOURNEY_MIN_SOURCE_REFS = 2

_AGENT_NAME = "location"


@dataclass(frozen=True)
class LocationFinding:
    """검증 결과 한 건."""

    code: str
    message: str
    severity: str  # "ERROR" | "WARNING" | "REVIEW"


def verify_location_result(
    result: AgentEventResult, request: TimelineDraftRequest
) -> AgentEventResult:
    """Location 결과를 입력과 대조해 문제를 warning 으로 덧붙인다(같은 객체를 돌려준다)."""

    if not request.stays and not request.movements:
        return result

    metrics = build_location_metrics(request)
    findings = [
        *_check_journey_coverage(result, metrics),
        *_check_coverage_uncertainty(result, metrics),
        *_check_transport_realism(result, metrics),
        *_check_raw_id_coverage(result, request),
        *_check_short_stay_scatter(result, metrics),
    ]
    if not findings:
        return result

    for finding in findings:
        result.warnings.append(
            AgentWarning(agent_name=_AGENT_NAME, message=finding.message)
        )
    logger.debug(
        "Location 결과 검증 지적",
        extra=log_fields(
            locationFindingCount=len(findings),
            locationFindingCodes=[f.code for f in findings],
        ),
    )
    return result


# --- 상위 여정 ------------------------------------------------------------------


def _long_distance_raw_ids(metrics: LocationMetrics) -> set[str]:
    return {
        m.raw_id
        for m in metrics.movements
        if m.distance_meters is not None and m.distance_meters >= LONG_DISTANCE_METERS
    }


def _check_journey_coverage(
    result: AgentEventResult, metrics: LocationMetrics
) -> list[LocationFinding]:
    """장거리 이동이 여러 건인데 그것들을 묶은 candidate 가 있는가."""

    long_distance = _long_distance_raw_ids(metrics)
    if len(long_distance) < LONG_DISTANCE_MIN_COUNT:
        return []

    for candidate in result.candidates:
        refs = {str(ref.raw_id) for ref in candidate.source_refs}
        if len(refs & long_distance) >= LONG_DISTANCE_MIN_COUNT:
            if len(refs) >= JOURNEY_MIN_SOURCE_REFS:
                return []

    return [
        LocationFinding(
            code="LOCATION_JOURNEY_MISSING",
            severity="WARNING",
            message=(
                f"장거리 이동 기록이 {len(long_distance)}건인데 이를 하나의 여정으로 묶은 "
                "후보가 없습니다. 출발지와 최종 도착지를 잇는 상위 여정이 빠졌을 수 있습니다."
            ),
        )
    ]


# --- 데이터 공백 ----------------------------------------------------------------


def _check_coverage_uncertainty(
    result: AgentEventResult, metrics: LocationMetrics
) -> list[LocationFinding]:
    """위치 기록이 끊겼는데 결과가 그 사실을 말하고 있는가.

    "공백을 언급했는가" 를 코드가 의미로 판정할 수는 없다. `uncertainty` 나 fragment 가
    하나라도 있으면 밝힌 것으로 본다 — 성긴 기준이라 거짓 음성이 난다(공백과 무관한
    uncertainty 로도 통과한다). 그래도 **아무 불확실성도 남기지 않은** 결과는 확실히
    잡는데, 실제로 문제가 되는 것은 그쪽이다.
    """

    if metrics.coverage_gap_minutes is None:
        return []

    mentioned = any(
        candidate.uncertainty for candidate in result.candidates
    ) or bool(result.fragments)
    if mentioned:
        return []

    return [
        LocationFinding(
            code="COVERAGE_UNCERTAINTY_MISSING",
            severity="ERROR",
            message=(
                f"마지막 위치 기록 이후 약 {metrics.coverage_gap_minutes:.0f}분의 공백이 "
                "있는데, 그 이후를 확정할 수 없다는 내용이 후보·단서 어디에도 없습니다."
            ),
        )
    ]


# --- 이동수단 현실성 ------------------------------------------------------------


def _check_transport_realism(
    result: AgentEventResult, metrics: LocationMetrics
) -> list[LocationFinding]:
    """속도로 설명되지 않는 이동수단 라벨을 그대로 근거로 삼았는가."""

    conflicted = {m.raw_id for m in metrics.movements if m.transport_conflict}
    if not conflicted:
        return []

    used = {
        str(ref.raw_id)
        for candidate in result.candidates
        if not candidate.uncertainty
        for ref in candidate.source_refs
    }
    overlap = conflicted & used
    if not overlap:
        return []

    return [
        LocationFinding(
            code="TRANSPORT_UNREALISTIC",
            severity="WARNING",
            message=(
                f"이동 {len(overlap)}건은 센서의 이동수단 라벨이 계산된 평균 속도로 "
                "설명되지 않는데, 근거 한계 없이 후보에 쓰였습니다."
            ),
        )
    ]


# --- rawId 보존 -----------------------------------------------------------------


def _check_raw_id_coverage(
    result: AgentEventResult, request: TimelineDraftRequest
) -> list[LocationFinding]:
    """입력 STAY·MOVEMENT 가 candidate 나 fragment 어딘가에 남아 있는가."""

    expected = {
        raw_id
        for item in [*request.stays, *request.movements]
        if (raw_id := raw_id_of(item)) is not None
    }
    covered = {
        str(ref.raw_id)
        for candidate in result.candidates
        for ref in candidate.source_refs
    } | {str(fragment.raw_id) for fragment in result.fragments}

    missing = expected - covered
    if not missing:
        return []

    return [
        LocationFinding(
            code="LOCATION_RAW_DROPPED",
            severity="ERROR",
            message=(
                f"입력 위치 기록 {len(missing)}건이 후보에도 단서에도 남지 않았습니다. "
                "그 시간대의 이동·체류가 하루에서 사라집니다."
            ),
        )
    ]


# --- 짧은 STAY 분산 -------------------------------------------------------------


def _check_short_stay_scatter(
    result: AgentEventResult, metrics: LocationMetrics
) -> list[LocationFinding]:
    """짧은 중간 STAY 가 각각 독립 방문 candidate 로 흩어졌는가."""

    short_stays = set(metrics.short_stay_raw_ids)
    if len(short_stays) < 2:
        return []

    solo = [
        candidate
        for candidate in result.candidates
        if len(candidate.source_refs) == 1
        and str(candidate.source_refs[0].raw_id) in short_stays
    ]
    if len(solo) < 2:
        return []

    return [
        LocationFinding(
            code="SHORT_STAY_SCATTERED",
            severity="REVIEW",
            message=(
                f"20분 이하로 머문 짧은 체류 {len(solo)}건이 각각 독립 후보가 됐습니다. "
                "이동 중 위치 분절이라면 하나의 여정으로 묶여야 합니다."
            ),
        )
    ]
