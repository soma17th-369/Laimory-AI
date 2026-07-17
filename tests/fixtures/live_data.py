"""날짜별 실제 입력 fixture 탐색과 로딩.

실제 LLM 테스트 입력은 ``data/input/<YYYY-MM-DD>/`` 아래에 날짜별로 둔다.
각 디렉터리는 같은 이름의 snapshot JSON과 그 JSON이 참조하는 사진 파일을 함께
보관한다. 테스트할 날짜는 인자로 지정하거나 ``LAIMORY_LIVE_DATA_DATE`` 환경변수로
바꿀 수 있다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path

from app.schemas import CollectedSnapshot, TimelineDraftRequest
from app.services.normalizer import normalize
from app.services.source_repository import load_snapshot_from_file

ROOT_DIR = Path(__file__).resolve().parents[2]
LIVE_INPUT_ROOT = ROOT_DIR / "data" / "input"
DEFAULT_LIVE_DATA_DATE = "2026-07-08"
LIVE_DATA_DATE_ENV = "LAIMORY_LIVE_DATA_DATE"
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class LiveDataCase:
    """하루치 snapshot과 사진을 함께 가리키는 실제 입력 fixture."""

    date: str
    directory: Path
    snapshot_path: Path

    @property
    def image_dir(self) -> Path:
        return self.directory

    def load_snapshot(self) -> CollectedSnapshot:
        return load_snapshot_from_file(self.snapshot_path)

    def load_request(self) -> TimelineDraftRequest:
        return normalize(self.load_snapshot())


def resolve_live_data_case(
    date: str | None = None,
    *,
    input_root: str | Path = LIVE_INPUT_ROOT,
) -> LiveDataCase:
    """지정한 날짜의 실제 입력 fixture를 검증해 반환한다."""

    selected_date = date or os.getenv(LIVE_DATA_DATE_ENV) or DEFAULT_LIVE_DATA_DATE
    try:
        if _DATE_PATTERN.fullmatch(selected_date) is None:
            raise ValueError(selected_date)
        date_type.fromisoformat(selected_date)
    except ValueError as exc:
        raise ValueError(
            f"{LIVE_DATA_DATE_ENV}는 YYYY-MM-DD 형식이어야 합니다: {selected_date}"
        ) from exc

    directory = Path(input_root) / selected_date
    snapshot_path = directory / f"{selected_date}.json"
    if not directory.is_dir():
        raise FileNotFoundError(f"날짜별 입력 디렉터리가 없습니다: {directory}")
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"날짜별 snapshot JSON이 없습니다: {snapshot_path}")

    return LiveDataCase(
        date=selected_date,
        directory=directory,
        snapshot_path=snapshot_path,
    )
