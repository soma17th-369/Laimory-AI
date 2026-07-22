"""staging MySQL 접속 스모크 체크.

`.env` 의 DB 접속 설정으로 실제 DB 에 붙어 입력·최종 타임라인 테이블을 확인한다. 사설망
DB 라 SSH 터널을 먼저 열어야 한다(터널을 열고 DB_HOST/DB_PORT 를 로컬 포트로
맞춘다). prod 는 VPC 직결이라 실제 host/port 를 쓴다.

실행:
    # (로컬) 다른 창에서 터널을 먼저 연다
    #   ssh -L 3307:10.0.32.12:3306 <ssh_user>@<bastion_host>
    uv run python scripts/db_smoke.py
    uv run python scripts/db_smoke.py --task-id <taskId>   # 특정 task 입력 조회까지

비밀번호는 출력하지 않는다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 스크립트를 직접 실행(`python scripts/db_smoke.py`)해도 repo 루트의 `app` 를
# 임포트할 수 있도록 sys.path 에 루트를 추가한다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.db import build_db_url
from app.core.db_models import (
    DailyRecord,
    DraftSourceItem,
    TimelineEvent,
    TimelineEventItem,
    TimelineItem,
)
from app.services.source_repository import MySQLSourceRepository


async def _run(task_id: str | None) -> int:
    url = build_db_url()
    # 비밀번호는 가린 채 접속 대상만 출력한다.
    print(f"접속 대상: {url.render_as_string(hide_password=True)}")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            print("접속 OK (SELECT 1)")

            for model, label in (
                (DraftSourceItem, "timeline_draft_source_items"),
                (DailyRecord, "daily_records"),
                (TimelineEvent, "timeline_events"),
                (TimelineItem, "timeline_items"),
                (TimelineEventItem, "timeline_event_items"),
            ):
                total = await conn.scalar(select(func.count()).select_from(model))
                print(f"  {label}: {total} rows")
    except Exception as exc:  # noqa: BLE001 - 스모크: 원인을 사람이 읽게 출력
        print(f"접속 실패: {exc}", file=sys.stderr)
        print(
            "→ SSH 터널이 열려 있는지, .env 의 DB_HOST/DB_PORT/DB_USER/DB_PASSWORD "
            "가 맞는지 확인하세요.",
            file=sys.stderr,
        )
        return 1
    finally:
        await engine.dispose()

    if task_id:
        snapshot = await MySQLSourceRepository().get(task_id)
        if snapshot is None:
            print(f"\ntaskId={task_id}: 입력 source 가 없습니다.")
        else:
            by_type: dict[str, int] = {}
            for item in snapshot.source_items:
                by_type[item.item_type.value] = by_type.get(item.item_type.value, 0) + 1
            print(
                f"\ntaskId={task_id}: source {len(snapshot.source_items)}건, "
                f"date={snapshot.record_date}, 타입별={by_type}"
            )

    return 0


def main() -> None:
    # Windows 콘솔에서 한글이 깨지지 않도록 UTF-8 출력을 강제한다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 재설정 불가 환경이면 그대로 둔다
            pass

    parser = argparse.ArgumentParser(description="staging MySQL 접속 스모크 체크")
    parser.add_argument(
        "--task-id",
        default=None,
        help="주면 해당 taskId 의 입력 source 조회 요약까지 출력",
    )
    args = parser.parse_args()

    if not settings.db_user or not settings.db_password:
        print(
            "경고: DB_USER 또는 DB_PASSWORD 가 비어 있습니다. .env 를 채우세요.",
            file=sys.stderr,
        )

    raise SystemExit(asyncio.run(_run(args.task_id)))


if __name__ == "__main__":
    main()
