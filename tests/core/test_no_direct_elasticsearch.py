"""애플리케이션이 Elasticsearch 를 직접 호출하지 않음을 정적으로 막는다 (이슈 #47).

운영 로그는 stdout 한 줄 JSON 으로만 나가고, Elasticsearch 로 옮기는 일은 EC2 의
별도 Filebeat 컨테이너가 한다. 앱에 ES URL·인증정보·`_bulk` 호출이 다시 들어오면
이슈 #47 이 되돌려진 것이므로 여기서 잡는다.

문자열 검색이라 우회할 방법은 얼마든지 있다. 목적은 실수로 되살아나는 것을 막는
것이지 악의적 우회를 막는 것이 아니다.
"""

import io
import re
import tokenize
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"

#: 앱 **코드**에 있으면 안 되는 패턴. 되살아나면 데이터 경계가 깨진 것이다.
#: 주석·docstring 은 검사하지 않는다 — 어디로 로그가 흐르는지 설명하려면 그 낱말들이
#: 필요하고, 설명은 Elasticsearch 를 호출하지 않는다.
FORBIDDEN_PATTERNS = (
    (r"_bulk", "Elasticsearch _bulk API 직접 호출"),
    (r"\bes_url\b", "Elasticsearch 접속 URL 설정"),
    (r"\bes_api_key\b", "Elasticsearch 자격증명 설정"),
    (r"\bes_event_index\b", "Elasticsearch 인덱스 설정"),
    (r"\bobs_enabled\b", "커스텀 관측 전송 마스터 스위치"),
    (r"\bobs_local_dir\b", "커스텀 관측 로컬 출력 설정"),
    (r"\belasticsearch\b", "Elasticsearch 클라이언트/모듈 참조"),
    (r"\bobservability\b", "제거된 커스텀 관측 패키지 참조"),
)


def _app_sources() -> list[Path]:
    return sorted(
        path
        for path in APP_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _code_tokens(path: Path) -> list[tuple[int, str]]:
    """주석과 문자열 리터럴을 걷어낸 (줄번호, 토큰) 목록을 돌려준다."""

    source = path.read_text(encoding="utf-8")
    return [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
        and token.string.strip()
    ]


@pytest.mark.parametrize(("pattern", "why"), FORBIDDEN_PATTERNS)
def test_app_code_has_no_elasticsearch_coupling(pattern: str, why: str) -> None:
    compiled = re.compile(pattern, re.IGNORECASE)
    hits = [
        f"{path.relative_to(APP_DIR.parent).as_posix()}:{lineno}"
        for path in _app_sources()
        for lineno, token in _code_tokens(path)
        if compiled.search(token)
    ]

    assert not hits, f"{why} 가 앱 코드에 남아 있습니다: {hits}"


#: 앱에서 httpx 를 직접 써도 되는 자리. 각 항목은 상대가 누구인지 분명해야 한다.
#: 여기 없는 파일이 HTTP 를 열면 그 자체가 리뷰 대상이다 — ES 직접 전송을 다른
#: HTTP 클라이언트로 재구현하는 것이 정확히 이 형태로 들어온다.
_ALLOWED_HTTPX_USERS = [
    # App Server 서버간 API(입력 조회·결과 저장·콜백).
    "app/services/app_server_client.py",
    # 사진 이미지 다운로드(#52). 상대는 allowlist 로 고정된 S3 이미지 호스트뿐이고,
    # 정책은 같은 파일의 `_reject_url` 이 소유한다.
    "app/agents/events/photo/image_source.py",
]


def test_httpx_is_only_used_by_known_integrations() -> None:
    """앱이 HTTP 로 말을 거는 상대는 명시된 둘뿐이어야 한다.

    ES 직접 전송을 다른 HTTP 클라이언트로 재구현하는 것도 같은 결합이다(제외 범위).
    Langfuse SDK 는 자체 전송 계층을 쓰므로 여기 잡히지 않는다.
    """

    users = [
        path.relative_to(APP_DIR.parent).as_posix()
        for path in _app_sources()
        if re.search(r"^\s*import httpx|^\s*from httpx", path.read_text(encoding="utf-8"), re.M)
    ]

    assert sorted(users) == sorted(_ALLOWED_HTTPX_USERS), users
