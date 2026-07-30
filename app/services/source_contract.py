"""입력 조회로 받은 수집 원본이 파이프라인 입력 계약을 지키는지 검증한다.

이슈 #40 이전에는 같은 검증을 ``timeline_draft_source_items`` 행에 대해 했다
(`source_repository.validate_source_rows`). 데이터 출처가 App Server API 로
바뀌면서 DB 행에만 있던 항목(`task_id`/`user_id` 컬럼 일관성)은 사라지고,
파이프라인이 실제로 의존하는 계약만 남았다.

형식 검증(rawId 가 UUID 인지, startAt 이 있는지)은 Pydantic 계약
(:class:`~app.schemas.source_snapshot.CollectedSourceItem`)이 이미 한다. 여기서는
**한 건씩 봐서는 알 수 없는 묶음 단위 규칙**만 본다.

예외 메시지는 (1) 어떤 규칙이 (2) 왜 깨졌고 (3) 어느 항목이 문제인지 특정할 수
있게 쓴다. 이 문장은 로그에만 남고 외부로는 안전 메시지(1102)만 나간다.
"""

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.schemas import CollectedSnapshot


class SourceBatchError(AppError, ValueError):
    """한 task 의 수집 원본 묶음이 파이프라인 입력 계약을 위반함.

    ``ValueError`` 도 함께 상속해 기존에 이 예외를 ``ValueError`` 로 잡던 호출부가
    그대로 동작하게 둔다.
    """

    default_code = ErrorCode.SOURCE_CONTRACT_VIOLATION


def ensure_source_contract(task_id: str, snapshot: CollectedSnapshot) -> None:
    """입력 조회 응답이 파이프라인 입력 계약을 지키는지 확인한다.

    Raises:
        SourceBatchError: 계약 위반(오류 코드 1102).
    """

    if snapshot.task_id != task_id:
        raise SourceBatchError(
            f"다른 task 의 입력이 왔습니다: 요청 taskId={task_id} 로 조회했으나 응답은 "
            f"taskId={snapshot.task_id} 입니다. 라우팅과 App Server 조회 조건을 확인하세요."
        )

    if not snapshot.source_items:
        raise SourceBatchError(
            f"수집 원본이 없습니다: taskId={task_id} 의 입력 조회 응답 sourceItems 가 "
            "0건입니다. App Server 가 해당 task 의 원본 적재를 마쳤는지 확인하세요."
        )

    raw_ids = [item.raw_id for item in snapshot.source_items]
    duplicates = sorted({raw_id for raw_id in raw_ids if raw_ids.count(raw_id) > 1})
    if duplicates:
        raise SourceBatchError(
            f"한 task 에 중복 rawId 가 있습니다: taskId={task_id}. rawId 는 원본의 정식 "
            f"식별자라 한 task 안에서 유일해야 합니다 — 중복 rawIds={duplicates}."
        )


def source_raw_ids(snapshot: CollectedSnapshot) -> set[str]:
    """이 task 의 유효한 근거 rawId 집합(결과 저장 전 자체검증 기준)."""

    return {item.raw_id for item in snapshot.source_items}
