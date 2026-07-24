"""진행 중인 백그라운드 처리 수를 세는 프로세스 로컬 게이지.

AgentCore Runtime 은 `GET /ping` 응답으로 컨테이너를 계속 살려둘지 판단한다.
`Healthy` 는 유휴, `HealthyBusy` 는 아직 처리 중이라는 뜻이다. AI 서버는 요청을
202 로 접수하고 실제 처리는 백그라운드에서 이어가므로, 응답을 이미 돌려준 뒤에도
작업이 남아 있으면 `HealthyBusy` 로 알려야 컨테이너가 회수되지 않는다.

이건 task 상태 저장소가 아니다. taskId 를 담지 않고, 조회 수단이 없고, 프로세스가
죽으면 함께 사라지는 순간 값이다. task 상태는 여전히 App Server 가 소유하며 AI 는
콜백으로만 통보한다.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_count = 0


@contextmanager
def track_inflight() -> Iterator[None]:
    """블록이 도는 동안 진행 중 처리로 센다.

    백그라운드 처리 함수를 감싸며, 예외로 빠져나가도 반드시 감소한다.
    """

    global _count

    with _lock:
        _count += 1
    try:
        yield
    finally:
        with _lock:
            _count -= 1


def inflight_count() -> int:
    """현재 진행 중인 백그라운드 처리 수."""

    with _lock:
        return _count


def is_busy() -> bool:
    """진행 중인 백그라운드 처리가 하나라도 있으면 True."""

    return inflight_count() > 0
