"""실제 LLM 테스트의 실행별 누적 출력 경로와 저장기."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from tests.fixtures.live_data import ROOT_DIR

LIVE_OUTPUT_ROOT = ROOT_DIR / "data" / "output" / "runs"
LIVE_RUN_ID_ENV = "LAIMORY_LIVE_RUN_ID"
_SAFE_COMPONENT = re.compile(r"[^0-9A-Za-z._+-]+")
_RUNS: dict[tuple[str, str, str, str], "LiveRunContext"] = {}
_RUNS_LOCK = Lock()


def _safe_component(value: str) -> str:
    normalized = _SAFE_COMPONENT.sub("-", value.strip()).strip("-.")
    return normalized or "unknown"


def _provider_and_model() -> tuple[str, str]:
    provider = os.getenv("LLM_PROVIDER", "unknown").lower()
    model = os.getenv(f"{provider.upper()}_MODEL", "unknown")
    return provider, model


@dataclass(frozen=True)
class LiveRunContext:
    """한 번의 실제 LLM 테스트 실행이 공유하는 출력 컨텍스트."""

    data_date: str
    provider: str
    model: str
    started_at: datetime
    run_id: str
    directory: Path

    def metadata(self) -> dict[str, str]:
        return {
            "dataDate": self.data_date,
            "startedAt": self.started_at.isoformat(),
            "provider": self.provider,
            "model": self.model,
            "runId": self.run_id,
        }

    def _prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        metadata_path = self.directory / "metadata.json"
        if not metadata_path.exists():
            metadata_path.write_text(
                json.dumps(self.metadata(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def path_for(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"run 출력 경로는 상대 경로여야 합니다: {relative}")
        return self.directory / relative

    def write_json(self, relative_path: str | Path, payload: dict[str, Any]) -> Path:
        self._prepare()
        output_path = self.path_for(relative_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return output_path

    def write_text(self, relative_path: str | Path, content: str) -> Path:
        self._prepare()
        output_path = self.path_for(relative_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        return output_path


def build_live_run_context(
    data_date: str,
    *,
    provider: str,
    model: str,
    started_at: datetime | None = None,
    run_id: str | None = None,
    output_root: str | Path = LIVE_OUTPUT_ROOT,
) -> LiveRunContext:
    """부작용 없이 실행별 출력 컨텍스트를 만든다."""

    started = started_at or datetime.now().astimezone()
    selected_run_id = run_id or started.strftime("%Y%m%dT%H%M%S.%f%z")
    directory_name = "-".join(
        (
            _safe_component(selected_run_id),
            _safe_component(provider.lower()),
            _safe_component(model),
        )
    )
    return LiveRunContext(
        data_date=data_date,
        provider=provider.lower(),
        model=model,
        started_at=started,
        run_id=selected_run_id,
        directory=Path(output_root) / data_date / directory_name,
    )


def current_live_run(data_date: str) -> LiveRunContext:
    """현재 프로세스에서 같은 날짜·모델이 공유할 실행 컨텍스트를 반환한다."""

    provider, model = _provider_and_model()
    requested_run_id = os.getenv(LIVE_RUN_ID_ENV, "")
    key = (data_date, provider, model, requested_run_id)
    with _RUNS_LOCK:
        context = _RUNS.get(key)
        if context is None:
            context = build_live_run_context(
                data_date,
                provider=provider,
                model=model,
                run_id=requested_run_id or None,
            )
            _RUNS[key] = context
        return context
