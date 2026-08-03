"""프롬프트 세트(v1/v2)의 완전성과 버전별 Agent 구조 계약 (#56).

`load_prompt` 는 요청한 파일이 없으면 다른 버전으로 대체하지 않고 실패한다. 그래서
어떤 버전이든 그 버전을 쓰는 Agent 가 읽는 파일이 **모두** 있어야 기동한다. 파일 하나가
빠지면 `PROMPT_VERSION` 을 바꾸는 순간 import 시점에 죽는다.

review 단계는 v1 전용이다. v2 는 단일 `complete_structured` 호출로 간다(#56).
이 계약이 깨지면 `PROMPT_VERSION=v1` 롤백이 더 이상 v1 동작을 되돌리지 못한다.
"""

from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: 각 Agent 디렉터리가 자기 프롬프트 세트에서 읽어야 하는 파일.
#: (agent 디렉터리, 버전별 필수 파일)
_REQUIRED_PROMPTS: dict[str, dict[str, set[str]]] = {
    "agents/timeline": {"v1": {"timeline.md"}, "v2": {"timeline.md"}},
    "agents/repair": {"v1": {"prompt.md"}, "v2": {"prompt.md"}},
    "agents/events/calendar": {"v1": {"prompt.md"}, "v2": {"prompt.md"}},
    "agents/events/notification": {"v1": {"prompt.md"}, "v2": {"prompt.md"}},
    "agents/events/location": {
        # review.md 는 v1 전용이다.
        "v1": {"prompt.md", "review.md"},
        "v2": {"prompt.md"},
    },
    "agents/events/sleep_activity": {
        "v1": {"prompt.md", "review.md"},
        "v2": {"prompt.md"},
    },
    "agents/events/photo": {
        # v1 은 메타데이터 fallback 도 LLM 으로 돌려 `describe_prompt.md` 를 읽는다.
        # v2 는 그 자리를 코드 생성으로 바꿔(#56 §12) 파일이 필요 없다.
        "v1": {"prompt.md", "describe_prompt.md", "describe_vision_prompt.md"},
        "v2": {"prompt.md", "describe_vision_prompt.md"},
    },
}

_VERSIONS = ("v1", "v2")


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("agent_dir", sorted(_REQUIRED_PROMPTS))
def test_prompt_set_has_every_file_the_agent_loads(agent_dir: str, version: str) -> None:
    prompt_dir = APP_ROOT / agent_dir / "prompts" / version
    missing = {
        name
        for name in _REQUIRED_PROMPTS[agent_dir][version]
        if not (prompt_dir / name).is_file()
    }

    assert not missing, (
        f"{agent_dir} 의 {version} 세트에 {sorted(missing)} 이(가) 없습니다. "
        f"load_prompt 는 다른 버전으로 대체하지 않으므로 PROMPT_VERSION={version} 기동이 실패합니다."
    )


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("agent_dir", sorted(_REQUIRED_PROMPTS))
def test_prompt_files_are_not_empty(agent_dir: str, version: str) -> None:
    prompt_dir = APP_ROOT / agent_dir / "prompts" / version

    for name in sorted(_REQUIRED_PROMPTS[agent_dir][version]):
        path = prompt_dir / name
        if not path.is_file():
            continue
        assert path.read_text(encoding="utf-8").strip(), f"{path} 가 비어 있습니다."


def test_v2_has_no_metadata_describe_prompt() -> None:
    """v2 의 메타데이터 fallback 은 코드가 만든다. 프롬프트가 있으면 안 쓰이는 파일이 된다."""

    path = APP_ROOT / "agents/events/photo/prompts/v2/describe_prompt.md"

    assert not path.exists(), (
        "photo v2 세트에 describe_prompt.md 가 있습니다. "
        "v2 의 메타데이터 description 은 MetadataPhotoDescriber 가 만듭니다(#56 §12)."
    )


@pytest.mark.parametrize(
    "agent_dir", ["agents/events/location", "agents/events/sleep_activity"]
)
def test_v2_has_no_review_prompt(agent_dir: str) -> None:
    """v2 에 review.md 를 다시 넣으면 v1 전용 분기와 어긋난다."""

    review = APP_ROOT / agent_dir / "prompts" / "v2" / "review.md"

    assert not review.exists(), (
        f"{agent_dir} 의 v2 세트에 review.md 가 있습니다. "
        "review 단계는 v1 전용이며 v2 는 단일 structured 호출로 갑니다(#56)."
    )


@pytest.mark.parametrize(
    "agent_dir", ["agents/events/location", "agents/events/sleep_activity"]
)
def test_review_prompt_keeps_draft_placeholder(agent_dir: str) -> None:
    """v1 review 프롬프트는 `{{DRAFT}}` 를 치환해 쓴다. 자리표시자가 없으면 초안이 사라진다."""

    review = APP_ROOT / agent_dir / "prompts" / "v1" / "review.md"

    assert "{{DRAFT}}" in review.read_text(encoding="utf-8")
