"""JSON 파일에서 수집 스냅샷을 읽는 테스트 헬퍼.

이슈 #40 이전에는 `app/services/source_repository.py` 가 이 함수를 갖고 있었다.
운영 경로는 이제 App Server 입력 조회 API 하나뿐이라 앱 코드에는 파일 로더가
필요 없고, live 입력 테스트와 e2e fixture 만 쓴다.
"""

import json
from pathlib import Path

from app.schemas import CollectedSnapshot


def load_snapshot_from_file(path: str | Path) -> CollectedSnapshot:
    """JSON 파일에서 `CollectedSnapshot` 을 읽어온다."""

    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8")
    return CollectedSnapshot.model_validate(
        _adapt_snapshot_payload(json.loads(raw), file_path)
    )


def _adapt_snapshot_payload(payload: dict, path: Path) -> dict:
    """JSON 파일 payload 를 CollectedSnapshot 입력으로 맞춘다.

    파일명(stem)을 기본 taskId 로 채운다. source item 의 rawId 는 파일에 이미
    있어야 한다(내부 id fallback 은 없다).
    """

    adapted = dict(payload)
    adapted.setdefault("taskId", path.stem)
    return adapted
