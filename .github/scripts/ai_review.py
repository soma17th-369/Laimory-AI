#!/usr/bin/env python3
"""PR AI 코드 리뷰 스크립트.

GitHub Actions 에서 실행되어 PR 의 변경 diff 를 OpenAI 로 리뷰하고,
결과를 라인별 인라인 코멘트가 포함된 PR 리뷰로 게시한다.

필요한 환경 변수:
- GITHUB_TOKEN   : PR 리뷰 게시용 토큰 (Actions 기본 토큰)
- REPO           : "owner/repo"
- PR_NUMBER      : PR 번호
- OPENAI_API_KEY : OpenAI API 키 (repo secret)
- MODEL          : 사용할 모델 (기본 gpt-4o-mini)
- MAX_DIFF_CHARS : 모델에 보낼 diff 최대 길이 (기본 45000)

외부 의존성은 `openai` 뿐이며, GitHub API 는 표준 라이브러리로 호출한다.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def env(name: str, required: bool = True, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        print(f"[ai-review] 환경 변수 {name} 가 필요합니다.", file=sys.stderr)
        sys.exit(1)
    return value or ""


TOKEN = env("GITHUB_TOKEN")
REPO = env("REPO")
PR_NUMBER = env("PR_NUMBER")
OPENAI_API_KEY = env("OPENAI_API_KEY")
MODEL = os.environ.get("MODEL", "gpt-4o-mini")
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "45000"))


def gh(method: str, path: str, payload: dict | None = None) -> object:
    """GitHub REST API 호출."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "ai-code-review",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"[ai-review] GitHub API 실패 {method} {path}: HTTP {exc.code} {detail}",
              file=sys.stderr)
        raise


def get_pr_files() -> list[dict]:
    """PR 의 변경 파일 목록(patch 포함)을 페이지네이션으로 모두 가져온다."""

    files: list[dict] = []
    page = 1
    while True:
        batch = gh("GET", f"/repos/{REPO}/pulls/{PR_NUMBER}/files?per_page=100&page={page}")
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def valid_right_lines(patch: str) -> set[int]:
    """unified diff patch 에서 인라인 코멘트가 가능한 신규 파일(RIGHT) 라인 집합.

    추가(`+`)/컨텍스트(` `) 라인만 신규 파일 라인 번호로 유효하다.
    삭제(`-`)는 신규 파일에 존재하지 않으므로 제외한다.
    """

    valid: set[int] = set()
    new_line: int | None = None
    for line in patch.splitlines():
        if line.startswith("@@"):
            # 예: @@ -12,7 +12,8 @@ def foo():
            try:
                plus = line.split("+", 1)[1]
                start = plus.split(",")[0].split(" ")[0]
                new_line = int(start)
            except (IndexError, ValueError):
                new_line = None
            continue
        if new_line is None:
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("+"):
            valid.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        else:  # 컨텍스트 라인
            valid.add(new_line)
            new_line += 1
    return valid


def build_diff_text(files: list[dict]) -> tuple[str, list[str]]:
    """모델에 보낼 diff 텍스트를 구성한다. MAX_DIFF_CHARS 를 넘으면 잘라낸다."""

    parts: list[str] = []
    included: list[str] = []
    total = 0
    for f in files:
        patch = f.get("patch")
        if not patch:  # 바이너리이거나 patch 가 없는 경우
            continue
        chunk = f"### FILE: {f['filename']}\n{patch}\n\n"
        if total + len(chunk) > MAX_DIFF_CHARS:
            break
        parts.append(chunk)
        included.append(f["filename"])
        total += len(chunk)
    return "".join(parts), included


SYSTEM_PROMPT = (
    "너는 꼼꼼한 시니어 코드 리뷰어야. 주어진 PR 의 diff 를 리뷰한다.\n"
    "- 버그, 보안 취약점, 성능 문제, 예외 처리 누락, 명백한 가독성 문제 위주로 실질적인 지적만 한다.\n"
    "- 사소한 취향/포매팅 지적은 하지 않는다.\n"
    "- 각 지적은 반드시 diff 에 등장한 파일 경로와, 그 파일의 '신규(변경 후) 기준 라인 번호'를 사용한다.\n"
    "- 추가되었거나 그대로 유지된(context) 라인에만 코멘트한다.\n"
    "- 모든 내용은 한국어로 작성한다.\n"
    "출력은 반드시 아래 JSON 형식만 반환한다:\n"
    '{"summary": "전체 요약(마크다운 가능)", '
    '"comments": [{"path": "파일경로", "line": 라인번호(int), "comment": "지적 내용"}]}\n'
    "지적할 것이 없으면 comments 는 빈 배열로 둔다."
)


def request_review(diff_text: str) -> dict:
    """OpenAI 로 리뷰를 요청하고 파싱한 결과(dict)를 반환한다."""

    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"다음 PR diff 를 리뷰해줘:\n\n{diff_text}"},
        ],
    )
    content = response.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"summary": content, "comments": []}


def main() -> int:
    files = get_pr_files()
    diff_text, included = build_diff_text(files)

    if not diff_text.strip():
        print("[ai-review] 리뷰할 코드 변경이 없습니다. 건너뜁니다.")
        return 0

    print(f"[ai-review] 리뷰 대상 파일 {len(included)}개, diff {len(diff_text)}자")
    result = request_review(diff_text)

    summary = (result.get("summary") or "").strip() or "AI 코드 리뷰 결과입니다."
    raw_comments = result.get("comments") or []

    # patch 에서 유효 라인만 남겨 GitHub API 422 를 방지한다.
    valid_map = {f["filename"]: valid_right_lines(f["patch"]) for f in files if f.get("patch")}
    inline: list[dict] = []
    dropped = 0
    for c in raw_comments:
        path = c.get("path")
        line = c.get("line")
        body = (c.get("comment") or "").strip()
        if path in valid_map and isinstance(line, int) and line in valid_map[path] and body:
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": body})
        else:
            dropped += 1

    footer = "\n\n---\n🤖 이 리뷰는 OpenAI 기반 자동 코드 리뷰입니다."
    if dropped:
        footer += f" (diff 밖 라인을 가리켜 {dropped}개 코멘트는 생략됨)"

    review: dict = {"event": "COMMENT", "body": summary + footer}
    if inline:
        review["comments"] = inline

    gh("POST", f"/repos/{REPO}/pulls/{PR_NUMBER}/reviews", review)
    print(f"[ai-review] 리뷰 게시 완료: 인라인 {len(inline)}개, 생략 {dropped}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
