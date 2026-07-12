"""타임라인 초안 task 상태 저장소.

짧은 수명의 처리 상태(PROCESSING/SUCCESS/FAILED)를 저장·조회한다. MVP 는
프로세스 내부 메모리에 저장하는 `InMemoryTaskStore` 를 쓰고, Redis 같은 외부
저장소 연동은 배포 단계 작업으로 유보한다. 그때는 이 `TaskStore` 인터페이스를
구현하는 클래스만 추가하고 `get_task_store()` 가 돌려주는 구현을 바꾸면 된다.
"""

from abc import ABC, abstractmethod
from functools import lru_cache
from threading import Lock

from app.schemas import TaskRecord, TaskStatus, TimelineDraft


class TaskStore(ABC):
    """task 상태 저장소 인터페이스."""

    @abstractmethod
    def create(self, task_id: str, transaction_id: str) -> TaskRecord:
        """`PROCESSING` 상태로 새 task 를 만든다."""

    @abstractmethod
    def get(self, task_id: str) -> TaskRecord | None:
        """task 스냅샷을 조회한다. 없으면 None."""

    @abstractmethod
    def mark_success(self, task_id: str, result: TimelineDraft) -> TaskRecord:
        """task 를 `SUCCESS` 로 바꾸고 결과를 저장한다."""

    @abstractmethod
    def mark_failed(self, task_id: str, error: str) -> TaskRecord:
        """task 를 `FAILED` 로 바꾸고 사유를 저장한다(결과는 저장하지 않는다)."""


class InMemoryTaskStore(TaskStore):
    """프로세스 메모리에 상태를 저장하는 기본 구현.

    여러 요청이 동시에 상태를 갱신할 수 있으므로 `Lock` 으로 감싼다.
    """

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._lock = Lock()

    def create(self, task_id: str, transaction_id: str) -> TaskRecord:
        record = TaskRecord(
            task_id=task_id,
            transaction_id=transaction_id,
            status=TaskStatus.PROCESSING,
        )
        with self._lock:
            self._records[task_id] = record
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def mark_success(self, task_id: str, result: TimelineDraft) -> TaskRecord:
        with self._lock:
            record = self._require(task_id)
            record.status = TaskStatus.SUCCESS
            record.result = result
            record.error = None
            return record

    def mark_failed(self, task_id: str, error: str) -> TaskRecord:
        with self._lock:
            record = self._require(task_id)
            record.status = TaskStatus.FAILED
            # 실패 시 partial 결과가 남지 않도록 result 를 비운다.
            record.result = None
            record.error = error
            return record

    def _require(self, task_id: str) -> TaskRecord:
        record = self._records.get(task_id)
        if record is None:
            raise KeyError(f"존재하지 않는 task 입니다: {task_id}")
        return record


@lru_cache
def get_task_store() -> TaskStore:
    """task 저장소 싱글턴을 반환한다.

    배포 단계에서 Redis 구현으로 교체할 때는 이 함수가 돌려주는 인스턴스만
    바꾸면 된다.
    """

    return InMemoryTaskStore()
