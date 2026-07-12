"""수집 스냅샷 저장소.

App Server 는 하루치 수집 데이터를 DB 에 적재하고 AI 서버에는 `taskId` 만
넘긴다. AI 서버는 이 저장소로 `taskId` 에 해당하는 `CollectedSnapshot` 을
읽어온다.

실제 DB 는 아직 정해지지 않아, `TaskStore` 와 동일하게 인터페이스만 고정하고
기본 구현으로 프로세스 메모리 저장소(`InMemorySourceRepository`)를 둔다.
SQL/Redis/HTTP 등 실제 저장소가 정해지면 `SourceRepository` 를 구현하는
클래스를 추가하고 `get_source_repository()` 가 돌려주는 구현만 바꾸면 된다.
"""

import json
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from threading import Lock

from app.schemas import CollectedSnapshot


class SourceRepository(ABC):
    """수집 스냅샷 저장소 인터페이스."""

    @abstractmethod
    def get(self, task_id: str) -> CollectedSnapshot | None:
        """`taskId` 에 해당하는 스냅샷을 조회한다. 없으면 None."""


class InMemorySourceRepository(SourceRepository):
    """프로세스 메모리에 스냅샷을 보관하는 기본 구현.

    로컬 개발/테스트에서 `put` 으로 스냅샷을 시드해 두고 조회한다. 여러 요청이
    동시에 접근할 수 있어 `Lock` 으로 감싼다.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, CollectedSnapshot] = {}
        self._lock = Lock()

    def put(self, snapshot: CollectedSnapshot) -> None:
        """스냅샷을 `taskId` 키로 저장한다(시드용)."""

        with self._lock:
            self._snapshots[snapshot.task_id] = snapshot

    def get(self, task_id: str) -> CollectedSnapshot | None:
        with self._lock:
            return self._snapshots.get(task_id)


def load_snapshot_from_file(path: str | Path) -> CollectedSnapshot:
    """JSON 파일에서 `CollectedSnapshot` 을 읽어온다(로컬 dev/e2e 시드용)."""

    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8")
    return CollectedSnapshot.model_validate(
        _adapt_snapshot_payload(json.loads(raw), file_path)
    )


def _adapt_snapshot_payload(payload: dict, path: Path) -> dict:
    adapted = dict(payload)
    adapted.setdefault("taskId", path.stem)

    source_items = []
    for index, item in enumerate(adapted.get("sourceItems", []), start=1):
        source_item = dict(item)
        source_item.setdefault("id", index)
        source_items.append(source_item)
    adapted["sourceItems"] = source_items
    return adapted


@lru_cache
def get_source_repository() -> SourceRepository:
    """수집 스냅샷 저장소 싱글턴을 반환한다.

    실제 DB 구현으로 교체할 때는 이 함수가 돌려주는 인스턴스만 바꾸면 된다.
    """

    return InMemorySourceRepository()
