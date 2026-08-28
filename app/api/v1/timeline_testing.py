"""동기 Timeline 테스트 엔드포인트 (이슈 #102).

`POST /v1/timeline/test` 는 Timeline 생성에 필요한 입력 전체를 JSON 으로 받아,
**요청 안에서** 파이프라인이 끝날 때까지 기다린 뒤 결과를 그대로 돌려준다. App Server
입력 조회·결과 저장·완료 콜백·`taskToken` 갱신을 하지 않으므로, 수집 원본 JSON 하나만
있으면 AI 결과를 바로 확인할 수 있다.

## 계약 재사용

- 요청 body 는 :class:`~app.schemas.timeline_input.TimelineInputPayload` 를 **상속한다**.
  입력 조회 응답이 쓰는 바로 그 필드 선언이라 계약이 갈릴 수가 없다. 여기서 다시 적는
  것은 `window` 를 필수로 좁히는 한 줄뿐이다.
- `taskId` 는 **App Server 가 발행하고 이 요청이 받는다.** 이 경로가 그것으로 무엇을
  조회하거나 저장하지는 않지만, 같은 taskId 로 돌린 비동기 실행과 로그·Langfuse 에서
  이어 볼 수 있어야 한다. `taskToken` 만 계약에서 빠진다 — 되부를 곳이 없어 쓸 데가 없다.
- 응답 body 는 **App Server 결과 저장 요청 계약 그대로**다
  (:class:`~app.schemas.timeline_result.TimelineResultRequest`). 실제로 저장됐을 값을
  그대로 본다. 다만 이 경로는 어디에도 저장하지 않는다 — 만든 것을 그대로 돌려줄 뿐이다.

## 노출 제한

`settings.timeline_test_endpoint_enabled` 가 참일 때만 동작한다(기본값은 `local`/`dev`).
막는 방법이 두 겹이다.

1. 비활성이면 :func:`ensure_timeline_test_enabled` 가 404 를 던져 **없는 경로와 같은**
   `1003` 이 나간다. 이 판단은 요청마다 하므로 설정이 곧 동작이다.
2. 비활성이면 OpenAPI 에도 싣지 않는다(`include_in_schema`). 이건 import 시점 값이라
   컨테이너 기동 시 환경으로 고정된다.

## 이 경로가 하지 않는 것

`POST /v1/timeline` 과 `POST /invocations` 의 접수·백그라운드 동작은 전혀 건드리지
않는다. 운영 호출 경로를 대체하지 않으며, 실제 LLM 을 부르므로 응답이 최대
`pipeline_timeout_sec` 까지 걸리고 토큰 비용이 발생한다.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.error_handlers import ERROR_RESPONSES
from app.api.request_logging import annotate_request_task
from app.core.config import settings
from app.schemas.timeline_input import TimelineInputPayload, TimelineInputWindow
from app.schemas.timeline_result import TimelineResultRequest
from app.services.timeline_testing import run_timeline_test

router = APIRouter()

#: 제한 시간이 끝나 개선을 마치지 못한 채 마지막 확정본을 돌려줬음을 알리는 헤더(#76).
#: body 로 싣지 않는 이유는 결과 저장 계약에 이 필드가 없기 때문이다 — 응답 JSON 이
#: 저장 요청과 한 글자도 다르지 않아야 "저장될 값"을 본다고 말할 수 있다.
TIMED_OUT_HEADER = "X-Timeline-Timed-Out"


class TimelineTestRequest(TimelineInputPayload):
    """동기 테스트 요청 body.

    필드는 :class:`~app.schemas.timeline_input.TimelineInputPayload` 가 선언한
    **그대로**다 — 입력 조회 응답이 쓰는 바로 그 선언이라, `taskId` 든 `sourceItems` 든
    `userMemory` 든 계약이 바뀌면 이 입구도 같이 바뀐다. 여기서 다시 적는 것은 한 줄뿐이다.

    `window` 는 **필수**다. 조회 응답에서는 선택이지만 이 경로에는 시간 창을 줄 다른
    통로가 없다(비동기 경로에서는 접수 요청이 그 자리를 맡는다).

    `taskToken` 은 **없다**. 토큰은 AI 서버가 App Server 를 되부를 때 쓰는 인증인데 이
    경로는 조회도 저장도 콜백도 하지 않는다. 그래서 그 필드만
    :class:`~app.schemas.timeline_input.TimelineInputResponse` 쪽에 있다.
    """

    window: TimelineInputWindow


def ensure_timeline_test_enabled() -> None:
    """비활성 환경에서는 이 경로가 아예 없는 것처럼 답한다.

    404 를 고른 것은 의도적이다. 403 은 "여기 뭔가 있는데 네가 못 볼 뿐" 을 알려 주고,
    운영에 테스트용 경로가 있다는 사실 자체가 알려질 이유가 없다.
    """

    if not settings.timeline_test_endpoint_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.post(
    "/test",
    response_model=TimelineResultRequest,
    status_code=status.HTTP_200_OK,
    responses=ERROR_RESPONSES,
    dependencies=[Depends(ensure_timeline_test_enabled)],
    include_in_schema=settings.timeline_test_endpoint_enabled,
    summary="동기 타임라인 생성 (테스트 전용)",
)
async def create_timeline_test_result(
    payload: TimelineTestRequest,
    http_request: Request,
    response: Response,
) -> TimelineResultRequest:
    """입력 JSON 으로 타임라인을 만들어 결과 저장 body 모양 그대로 돌려준다.

    만든 것을 어디에도 저장하지 않고 응답으로만 내보낸다.

    실패는 공통 오류 계약을 그대로 따른다 — 수집 원본 계약 위반은 `1102`, 구조화 출력
    실패는 `1202`, 저장 전 자체검증 실패는 `1301`, 확정본 없는 제한 시간 초과는 `1201`
    이다. 새 코드를 만들지 않는다.
    """

    # 실행보다 **먼저** 적는다. 처리 중에 실패해도 그 요청 로그에 taskId 가 남아야
    # 비동기 경로의 같은 taskId 기록과 이어 볼 수 있다.
    annotate_request_task(http_request, payload.task_id)
    run = await run_timeline_test(
        payload,
        window_start=payload.window.start_at,
        window_end=payload.window.end_at,
    )
    if run.timed_out:
        response.headers[TIMED_OUT_HEADER] = "true"
    return run.result
