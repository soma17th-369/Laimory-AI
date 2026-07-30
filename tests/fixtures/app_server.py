"""App Server 클라이언트 테스트 더블.

운영 경로의 유일한 외부 의존이라 대부분의 단위 테스트가 이걸 주입한다. 호출을
기록하되 **토큰은 호출 시점의 값**을 함께 남긴다 — 갱신된 토큰이 다음 호출에
실제로 반영됐는지 보려면 나중에 홀더를 읽어서는 알 수 없다.
"""

from dataclasses import dataclass

from app.core.error_codes import ErrorCode
from app.schemas import CollectedSnapshot, TimelineCallbackPayload
from app.schemas.timeline_result import TimelineResultRequest
from app.services.app_server_client import AppServerClient, AppServerError, TaskToken


@dataclass
class RecordedCall:
    """한 번의 App Server 호출."""

    task_id: str
    token: str
    payload: object = None


class FakeAppServerClient(AppServerClient):
    """호출을 기록하고, 지정한 실패를 재현하는 스텁."""

    def __init__(
        self,
        snapshot: CollectedSnapshot | None = None,
        *,
        fetch_error: Exception | None = None,
        submit_error: Exception | None = None,
        callback_ok: bool = True,
        fetch_token: str | None = None,
        submit_token: str | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._fetch_error = fetch_error
        self._submit_error = submit_error
        self._callback_ok = callback_ok
        # 응답 body 로 새 토큰을 내려주는 상황을 재현한다.
        self._fetch_token = fetch_token
        self._submit_token = submit_token

        self.fetch_calls: list[RecordedCall] = []
        self.submit_calls: list[RecordedCall] = []
        self.callback_calls: list[RecordedCall] = []
        #: 호출 순서. "결과 저장 뒤에 콜백" 같은 순서 계약을 여기서 본다.
        self.order: list[str] = []

    async def fetch_input(self, task_id: str, token: TaskToken) -> CollectedSnapshot:
        self.fetch_calls.append(RecordedCall(task_id, token.value))
        self.order.append("input")
        if self._fetch_error is not None:
            raise self._fetch_error
        if self._fetch_token is not None:
            token.absorb({"taskToken": self._fetch_token})
        if self._snapshot is None:
            raise AppServerError(
                f"입력이 없습니다: taskId={task_id}",
                code=ErrorCode.SOURCE_SNAPSHOT_NOT_FOUND,
                abort=True,
            )
        return self._snapshot

    async def submit_result(
        self,
        task_id: str,
        token: TaskToken,
        request: TimelineResultRequest,
    ) -> None:
        self.submit_calls.append(RecordedCall(task_id, token.value, request))
        self.order.append("result")
        if self._submit_error is not None:
            raise self._submit_error
        if self._submit_token is not None:
            token.absorb({"taskToken": self._submit_token})

    async def send_callback(
        self,
        task_id: str,
        token: TaskToken,
        payload: TimelineCallbackPayload,
    ) -> bool:
        self.callback_calls.append(RecordedCall(task_id, token.value, payload))
        self.order.append("callback")
        return self._callback_ok

    # --- 조회 헬퍼 ------------------------------------------------------

    @property
    def last_callback(self) -> TimelineCallbackPayload | None:
        if not self.callback_calls:
            return None
        return self.callback_calls[-1].payload  # type: ignore[return-value]

    @property
    def last_result(self) -> TimelineResultRequest | None:
        if not self.submit_calls:
            return None
        return self.submit_calls[-1].payload  # type: ignore[return-value]
