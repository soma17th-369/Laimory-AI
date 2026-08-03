"""환경 설정으로 선택한 Agent 프롬프트 파일을 읽는다."""

from pathlib import Path

from app.core.config import settings


def load_prompt(
    module_file: str | Path,
    filename: str,
    *,
    version: str | None = None,
) -> str:
    """호출 Agent의 ``prompts/{version}/{filename}``을 UTF-8로 읽는다.

    ``version``은 단위 테스트와 도구에서 명시적으로 확인할 때만 사용한다. 제품
    호출부는 전역 ``PROMPT_VERSION``을 따라 모든 Agent가 같은 세트를 선택한다.
    요청한 파일이 없을 때 다른 버전으로 대체하지 않는다.
    """

    selected_version = str(version or settings.prompt_version).strip().lower()
    if not selected_version or Path(selected_version).name != selected_version:
        raise ValueError("프롬프트 버전은 단일 경로 이름이어야 합니다.")
    if not filename or Path(filename).name != filename:
        raise ValueError("프롬프트 파일명은 단일 경로 이름이어야 합니다.")

    prompt_path = (
        Path(module_file).resolve().parent
        / "prompts"
        / selected_version
        / filename
    )
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "프롬프트 파일이 없습니다 "
            f"(version={selected_version}, file={filename})."
        ) from exc
