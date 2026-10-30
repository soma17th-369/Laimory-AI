"""환경 설정으로 선택한 Agent 프롬프트 파일을 읽는다."""

from pathlib import Path

from app.core.config import settings

#: #119 이전의 계약으로 도는 프롬프트 세트. 이 세트들은 확정 pass 의 새 검사(이동 사이
#: 장시간 체류, 대화 event 개수, 타입별 지속시간)와 Repair 의 새 입력·도구를 쓰지 않는다.
#:
#: v2 는 운영 세트다. 프롬프트가 설명하지 않는 warning·입력·도구를 코드가 먼저 주면 운영
#: 결과가 달라진다. 새 세트는 이름을 여기 더하지 않는 한 새 계약으로 돈다.
#:
#: User Memory 갱신도 같은 기준으로 가른다(#121). 이 세트들은 문서 전체를 다시 출력하고
#: 성향 근거를 memo 로 제한한다. 새 세트는 바꿀 항목만 출력한다.
LEGACY_PROMPT_VERSIONS = frozenset({"v1", "v2"})


def uses_legacy_contract(version: str | None = None) -> bool:
    """이 프롬프트 세트가 #119 이전의 계약으로 도는가.

    문자열 대소 비교(`< "v3"`)는 `"v10" < "v3"` 이 참이라 쓰지 않는다.
    """

    selected = str(version or settings.prompt_version).strip().lower()
    return selected in LEGACY_PROMPT_VERSIONS


#: 데이터별 단서 계약(#167~#171)보다 앞선 세트. v4 부터는 Event Agent 가 공통 candidate
#: 대신 데이터별 단서를 내고, 위치·일정은 코드가 뼈대와 단서로 만든다.
#:
#: v3 는 운영 세트다. v4 에서만 바꿀 동작은 이 판정으로 가르고 v3 경로는 그대로 둔다.
#: v4 는 `uses_legacy_contract()` 가 거짓이라 v3 의 #119 이후 계약도 그대로 이어받는다.
PRE_CLUE_PROMPT_VERSIONS = frozenset({"v1", "v2", "v3"})


def uses_clue_contract(version: str | None = None) -> bool:
    """이 프롬프트 세트가 데이터별 단서 계약(v4~)으로 도는가."""

    selected = str(version or settings.prompt_version).strip().lower()
    return selected not in PRE_CLUE_PROMPT_VERSIONS


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
