"""환경 버전 기반 Agent 프롬프트 로더 검증."""

from pathlib import Path

import pytest

from app.agents.prompt_loader import load_prompt


def _write_prompt(root: Path, version: str, content: str) -> Path:
    module_file = root / "agent.py"
    prompt_dir = root / "prompts" / version
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "prompt.md").write_text(content, encoding="utf-8")
    return module_file


def test_load_prompt_reads_requested_version(tmp_path: Path) -> None:
    module_file = _write_prompt(tmp_path, "v1", "v1 프롬프트")

    assert load_prompt(module_file, "prompt.md", version="v1") == "v1 프롬프트"


def test_load_prompt_does_not_fallback_to_v1(tmp_path: Path) -> None:
    module_file = _write_prompt(tmp_path, "v1", "v1 프롬프트")

    with pytest.raises(FileNotFoundError, match=r"version=v2"):
        load_prompt(module_file, "prompt.md", version="v2")


@pytest.mark.parametrize("filename", ["../prompt.md", "nested/prompt.md"])
def test_load_prompt_rejects_nested_filename(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError):
        load_prompt(tmp_path / "agent.py", filename, version="v1")
