"""수집 스냅샷 저장소.

App Server 는 하루치 수집 데이터를 `timeline_draft_source_items` 테이블에 적재하고
AI 서버에는 `taskId` 만 넘긴다. AI 서버는 이 저장소로 `taskId` 에 해당하는
`CollectedSnapshot` 을 읽어온다.

구현은 두 가지다.

- `MySQLSourceRepository`: 실제 staging DB(`timeline_draft_source_items`)에서 읽는다.
  DB 는 필수이므로 `get_source_repository()` 는 항상 이걸 돌려준다.
- `InMemorySourceRepository`: 프로세스 메모리 스텁. 단위 테스트가 의존성 오버라이드로
  직접 주입해 쓰며, 운영 경로에서는 쓰지 않는다.

DB 접근이 async 이므로 `SourceRepository.get` 은 async 다.
"""

import json
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from threading import Lock

from sqlalchemy import select

from app.core.db import session_scope
from app.core.db_models import DraftSourceItem
from app.schemas import CollectedSnapshot, CollectedSourceItem
from app.schemas.source_snapshot import TimelineWindow

_DEFAULT_TIMEZONE = "Asia/Seoul"


class SourceBatchError(ValueError):
    """한 task의 staging source 묶음이 파이프라인 입력 계약을 위반함."""


def validate_source_rows(task_id: str, rows: list[DraftSourceItem]) -> int:
    """source 묶음이 파이프라인 입력 계약을 지키는지 검증하고 user_id를 반환한다.

    각 예외 메시지는 (1) 어떤 규칙이 (2) 왜 깨졌고 (3) 어느 행이 문제인지 바로
    특정할 수 있도록, taskId 와 함께 위반 행 식별자(sourceItemId/rawId 등)를 담는다.
    """

    if not rows:
        raise SourceBatchError(
            f"수집 원본이 없습니다: taskId={task_id} 로 조회된 "
            "timeline_draft_source_items 행이 0건입니다. App Server 가 해당 task 의 "
            "원본 적재를 마쳤는지, taskId 가 올바른지 확인하세요."
        )

    foreign_ids = sorted({row.task_id for row in rows if row.task_id != task_id})
    if foreign_ids:
        raise SourceBatchError(
            f"다른 task 의 source item 이 섞여 있습니다: 요청 taskId={task_id} 로 "
            f"조회했으나 다른 task 의 행이 포함됐습니다(foreignTaskIds={foreign_ids}). "
            "한 묶음은 한 task 에만 속해야 합니다 — 조회 조건과 적재 정합성을 확인하세요."
        )

    user_ids = {row.user_id for row in rows}
    if len(user_ids) != 1:
        raise SourceBatchError(
            f"한 task 에 여러 user_id 가 섞여 있습니다: taskId={task_id} 의 source item 은 "
            f"한 사용자 소유여야 하는데 userIds={sorted(user_ids)} 로 여러 명입니다. "
            "적재 데이터 정합성을 확인하세요."
        )

    missing_time_ids = [
        row.timeline_draft_source_item_id for row in rows if row.start_at is None
    ]
    if missing_time_ids:
        raise SourceBatchError(
            f"start_at 이 없는 source item 이 있습니다: taskId={task_id}. 모든 원본은 "
            "시각이 있어야 타임라인에 배치할 수 있습니다 — start_at 이 비어 있는 "
            f"sourceItemIds={missing_time_ids}."
        )

    empty_raw_item_ids = [
        row.timeline_draft_source_item_id for row in rows if not row.raw_id
    ]
    if empty_raw_item_ids:
        raise SourceBatchError(
            f"raw_id 가 없는 source item 이 있습니다: taskId={task_id}. raw_id 는 원본의 "
            "정식 식별자라 비어 있으면 안 됩니다 — raw_id 가 빈 "
            f"sourceItemIds={empty_raw_item_ids}."
        )

    raw_ids = [row.raw_id for row in rows]
    duplicate_raw_ids = sorted(
        raw_id for raw_id in set(raw_ids) if raw_ids.count(raw_id) > 1
    )
    if duplicate_raw_ids:
        # 어느 행들이 같은 raw_id 를 갖는지 함께 보여 문제 위치를 특정하기 쉽게 한다.
        duplicates_by_raw = {
            raw_id: [
                row.timeline_draft_source_item_id
                for row in rows
                if row.raw_id == raw_id
            ]
            for raw_id in duplicate_raw_ids
        }
        raise SourceBatchError(
            f"한 task 에 중복 raw_id 가 있습니다: taskId={task_id}. 같은 task 안에서 "
            "raw_id 는 유일해야 합니다(저장 시 디듀프 기준) — 중복 "
            f"raw_id→sourceItemIds={duplicates_by_raw}."
        )

    return next(iter(user_ids))


class SourceRepository(ABC):
    """수집 스냅샷 저장소 인터페이스."""

    @abstractmethod
    async def get(self, task_id: str) -> CollectedSnapshot | None:
        """`taskId` 에 해당하는 스냅샷을 조회한다. 없으면 None."""


class InMemorySourceRepository(SourceRepository):
    """프로세스 메모리에 스냅샷을 보관하는 스텁 구현.

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

    async def get(self, task_id: str) -> CollectedSnapshot | None:
        with self._lock:
            return self._snapshots.get(task_id)


class MySQLSourceRepository(SourceRepository):
    """staging DB(`timeline_draft_source_items`)에서 스냅샷을 읽는 구현."""

    async def get(self, task_id: str) -> CollectedSnapshot | None:
        async with session_scope() as session:
            result = await session.execute(
                select(DraftSourceItem)
                .where(DraftSourceItem.task_id == task_id)
                .order_by(DraftSourceItem.timeline_draft_source_item_id)
            )
            rows = list(result.scalars().all())

        if not rows:
            return None
        return _rows_to_snapshot(task_id, rows)


def _rows_to_snapshot(
    task_id: str, rows: list[DraftSourceItem]
) -> CollectedSnapshot:
    """`timeline_draft_source_items` 행들을 `CollectedSnapshot` 으로 조립한다.

    이 테이블엔 record_date/timezone/window/user_memory 컬럼이 없어, 날짜와
    시간 창은 각 행의 `start_at`/`end_at` 에서 파생하고 timezone 은 기본값을 쓴다.
    user_memory 는 이 경로로는 없으므로 None 이다. 시각은 파이프라인 관례대로
    ISO 문자열로 유지한다(naive; 수집 원본 포맷과 동일).
    """

    validate_source_rows(task_id, rows)

    items: list[CollectedSourceItem] = []
    for row in rows:
        try:
            items.append(
                CollectedSourceItem(
                    raw_id=row.raw_id,
                    item_type=row.item_type,
                    start_at=row.start_at.isoformat(),
                    end_at=row.end_at.isoformat() if row.end_at else None,
                    payload=row.payload or {},
                )
            )
        except Exception as exc:  # noqa: BLE001 - batch 전체 실패로 원본 누락 방지.
            raise SourceBatchError(
                "source item 변환 실패: "
                f"taskId={task_id}, sourceItemId={row.timeline_draft_source_item_id}, "
                f"rawId={row.raw_id}, itemType={row.item_type}, error={exc}"
            ) from exc

    starts = [row.start_at for row in rows if row.start_at is not None]
    ends = [row.end_at for row in rows if row.end_at is not None]

    record_date = min(starts).date().isoformat()
    window_start = min(starts)
    window_end = max([*ends, *starts])
    window = TimelineWindow(
        start_time=window_start.isoformat(),
        end_time=window_end.isoformat(),
    )

    return CollectedSnapshot(
        task_id=task_id,
        record_date=record_date,
        record_time_zone=_DEFAULT_TIMEZONE,
        user_memory=None,
        timeline_window=window,
        source_items=items,
    )
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

    DB 는 필수이므로 항상 MySQL 저장소를 돌려준다. `InMemorySourceRepository` 는
    단위 테스트가 의존성 오버라이드로 직접 주입해 쓰는 스텁이다.
    """

    return MySQLSourceRepository()
