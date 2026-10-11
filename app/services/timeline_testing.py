"""동기 Timeline 테스트 실행부 (이슈 #102).

`POST /v1/timeline/test` 가 요청 안에서 끝까지 기다리며 부르는 경로다. 하는 일은
**타임라인을 만드는 것뿐**이고, App Server 와는 한 번도 통신하지 않는다.

    입력 조회 없음 · 결과 저장 없음 · 완료 콜백 없음 · `taskToken` 갱신 없음

수집 원본이 요청 body 로 들어오므로 :mod:`app.services.timeline_runner` 의 1단계
(입력 조회)가 통째로 빠지고, 결과를 돌려주는 것이 곧 종료라 4~5단계(저장·콜백)도
없다. 그 사이의 처리는 **같은 함수들을 그대로 부른다** — `ensure_source_contract`,
`normalize`, `run_main_agent`, `reject_empty_structured_failure`,
`ensure_timeline_valid_for_storage`, `build_result_request`. 테스트용 파이프라인을
따로 만들지 않는다.

runner 를 쪼개서 공유하지 않은 이유는, runner 에 남는 것이 대부분 **저장·콜백·토큰·
관측 구조**여서다. 그 부분이 이 경로에는 하나도 해당하지 않고, 억지로 합치면 저장
순서 계약(#40)이 두 벌로 갈린다.

## 제한 시간

`pipeline_timeout_sec` 를 그대로 적용한다. 초과했을 때의 처리도 production 과 같다
(#76). Repair 가 확정할 때마다 발행하는 draft 를 받아 두었다가, 제한 시간이 끝나면
마지막 확정본으로 결과를 만들고 :attr:`TimelineTestRun.timed_out` 을 세운다. 확정본이
하나도 없을 때만 실패다(`1201`). 이 경로의 목적이 "실제 저장될 결과"를 보여 주는
것이므로, production 이 저장할 값과 다른 것을 보여 주면 안 된다.

## 관측

`execution_context` 로 감싼다. 로그 상관키(`taskId`) 때문만이 아니라
`reject_empty_structured_failure` 가 읽는 구조화 실패 목록이 이 컨텍스트에 쌓이기
때문이다 — 열지 않으면 #98 가드가 항상 통과한다.

`track_inflight` 로도 감싼다. 이 요청은 최대 `pipeline_timeout_sec` 동안 돌므로,
세지 않으면 처리 도중 `GET /ping` 이 `Healthy` 를 답해 컨테이너가 회수될 수 있다.
"""

import asyncio
from dataclasses import dataclass

from app.agents.main import run_main_agent
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.core.execution_context import ExecutionStage, execution_context
from app.core.inflight import track_inflight
from app.core.logging import get_logger, log_fields
from app.schemas import TimelineDraft
from app.schemas.source_snapshot import TimelineWindow
from app.schemas.timeline_input import TimelineInputPayload
from app.schemas.timeline_result import TimelineResultRequest
from app.services.normalizer import normalize
from app.services.source_contract import (
    ensure_source_contract,
    resolve_user_memory,
    source_raw_ids,
)
from app.services.timeline_result import build_result_request
from app.services.timeline_runner import reject_empty_structured_failure
from app.services.timeline_validator import ensure_timeline_valid_for_storage

logger = get_logger(__name__)


@dataclass(frozen=True)
class TimelineTestRun:
    """동기 실행 결과.

    ``result`` 는 production 이 App Server 로 보냈을 body 와 같은 값이다.
    ``timed_out`` 은 제한 시간이 끝나 개선을 마치지 못한 채 마지막 확정본을 돌려줬는지를
    말한다 — 실패가 아니다(#76). 결과 저장 계약에는 이 필드가 없으므로 응답 body 가
    아니라 헤더로 나간다.
    """

    result: TimelineResultRequest
    timed_out: bool


async def run_timeline_test(
    payload: TimelineInputPayload,
    *,
    window_start: str,
    window_end: str,
) -> TimelineTestRun:
    """입력을 받아 그 자리에서 타임라인을 만들고 결과 저장 body 를 돌려준다.

    스냅샷으로 옮기고 계약을 확인하는 순서는
    :meth:`~app.services.app_server_client.HttpAppServerClient.fetch_input` 과 같다.
    다른 것은 그 값이 HTTP 응답이 아니라 요청 body 로 왔다는 것뿐이다.

    Args:
        payload: 입력 한 벌. 입력 조회 응답과 **같은 필드 선언**을 쓴다.
        window_start: 대상 시간 창 시작. 요청이 정본이라 스냅샷 값을 덮어쓴다.
        window_end: 대상 시간 창 끝.

    Raises:
        SourceBatchError: 수집 원본 묶음이 입력 계약을 어겼다(1102).
        StructuredOutputError: 구조화 출력 실패로 event 가 하나도 없다(1202).
        TimelineValidationError: 결과가 입력에 없는 rawId 를 참조한다(1301).
        AppError: 제한 시간 안에 확정본을 하나도 만들지 못했다(1201).
    """

    task_id = payload.task_id
    with track_inflight(), execution_context(task_id):
        # 컨텍스트를 먼저 연다. `resolve_user_memory` 의 1106 진단도 같은 taskId 로
        # 남아야 한다.
        snapshot = payload.to_snapshot(user_memory=resolve_user_memory(payload))
        ensure_source_contract(task_id, snapshot)

        # 요청이 준 window 를 정본으로 덮어쓴다. 비동기 경로와 같은 규칙이다.
        snapshot = snapshot.model_copy(
            update={
                "timeline_window": TimelineWindow(
                    start_time=window_start,
                    end_time=window_end,
                )
            }
        )
        request = normalize(snapshot)

        confirmed: list[TimelineDraft] = []
        timed_out = False
        try:
            draft = await asyncio.wait_for(
                run_main_agent(request, on_confirm=confirmed.append),
                timeout=settings.pipeline_timeout_sec,
            )
        except asyncio.TimeoutError as exc:
            if not confirmed:
                # 돌려줄 것이 없다. 비동기 경로가 저장하지 못하는 상황과 같은 코드다.
                raise AppError(
                    "제한 시간 안에 확정된 draft 가 없어 결과를 만들지 못했습니다: "
                    f"taskId={snapshot.task_id}, timeoutSec={settings.pipeline_timeout_sec}",
                    code=ErrorCode.PIPELINE_TIMEOUT,
                ) from exc
            timed_out = True
            draft = confirmed[-1]
            logger.warning(
                "제한 시간 초과: 마지막 확정본으로 동기 테스트 결과를 만듭니다.",
                extra=log_fields(
                    taskId=snapshot.task_id,
                    stage=ExecutionStage.MAIN_AGENT.value,
                    timeoutSec=settings.pipeline_timeout_sec,
                    confirmedDraftCount=len(confirmed),
                    eventCount=len(draft.events),
                ),
            )

        reject_empty_structured_failure(request, draft)
        ensure_timeline_valid_for_storage(draft, source_raw_ids(snapshot))
        return TimelineTestRun(
            result=build_result_request(draft),
            timed_out=timed_out,
        )
