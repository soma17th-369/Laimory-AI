"""App Server 콜백 전송.

파이프라인 처리가 끝나면(성공/실패 모두) 결과를 App Server 의 콜백 URL 로
POST 한다. 콜백 전송 자체의 실패(네트워크 오류, 4xx/5xx 등)는 서버를 중단시키지
않고 로그로만 남긴다. task 상태는 App Server 가 소유하며(AI 서버는 보관하지 않음),
실제 결과는 App Server 가 staging DB 에서 읽는다.
"""

from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas import TimelineCallbackPayload

logger = get_logger(__name__)

TIMELINE_DRAFT_CALLBACK_PATH = "/timeline/drafts/{taskId}/callback"


def build_callback_url(app_server_api_url: str, task_id: str) -> str:
    """App Server API 기본 URL과 task ID로 완료 콜백 URL을 만든다."""

    encoded_task_id = quote(task_id, safe="")
    callback_path = TIMELINE_DRAFT_CALLBACK_PATH.replace("{taskId}", encoded_task_id)
    return f"{app_server_api_url.rstrip('/')}{callback_path}"


async def send_callback(
    app_server_api_url: str,
    task_id: str,
    callback_token: str,
    payload: TimelineCallbackPayload,
) -> bool:
    """task별 콜백 URL로 인증 헤더와 결과 payload를 POST 한다.

    Returns:
        전송 성공 여부. 실패해도 예외를 던지지 않고 False 를 반환한다.
    """

    url = build_callback_url(app_server_api_url, task_id)
    body = payload.model_dump(by_alias=True, mode="json")
    try:
        async with httpx.AsyncClient(timeout=settings.callback_timeout_sec) as client:
            response = await client.post(
                url,
                headers={"Callback-Token": callback_token},
                json=body,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "콜백 전송 실패: taskId=%s, url=%s, error=%s",
            task_id,
            url,
            exc,
        )
        return False

    logger.info(
        "콜백 전송 완료: taskId=%s, status=%s, url=%s",
        task_id,
        payload.status.value,
        url,
    )
    return True
