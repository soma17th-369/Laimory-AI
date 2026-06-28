#!/usr/bin/env python3
"""issue-work 스킬 헬퍼.

이슈 번호(인자로 받거나 현재 브랜치명에서 추출)에 해당하는 GitHub 이슈
내용을 가져와 실행계획 수립에 필요한 정보를 JSON으로 출력한다.
판단(실행계획 수립, 사용자와 대화하며 작업 진행)은 SKILL.md를 따르는
Claude가 담당하고, 이 스크립트는 이슈 데이터 조회만 수행한다.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# .agents/skills/issue-work/scripts/issue_work.py 기준 저장소 루트
ROOT = Path(__file__).resolve().parents[4]


def run_git(args):
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def github_token():
    value = os.environ.get("GITHUB_TOKEN")
    if value:
        return value.strip()
    env_local = ROOT / ".env.local"
    if env_local.exists():
        for line in env_local.read_text(encoding="utf-8").splitlines():
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(
        "GITHUB_TOKEN을 찾을 수 없습니다. 환경 변수 GITHUB_TOKEN을 설정하거나 "
        "저장소 루트의 .env.local에 GITHUB_TOKEN=ghp_xxx 형식으로 추가하세요. "
        ".env.local은 커밋하지 마세요."
    )


def api(method, path, allow_errors=False):
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {github_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read().decode("utf-8")
            return json.loads(data) if data else None
    except urllib.error.HTTPError as exc:
        if allow_errors:
            return None
        message = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API 실패: HTTP {exc.code} {message}") from exc
    except urllib.error.URLError as exc:
        if allow_errors:
            return None
        raise SystemExit(f"GitHub API 연결 실패: {exc.reason}") from exc


def branch():
    return run_git(["branch", "--show-current"])


def branch_issue_number(branch_name):
    match = re.search(r"#(\d+)", branch_name)
    if not match:
        return None
    return int(match.group(1))


def repo_slug():
    remote = run_git(["remote", "get-url", "origin"])
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", remote)
    if not match:
        raise SystemExit(f"origin 원격에서 GitHub 저장소를 해석할 수 없습니다: {remote}")
    return match.group("owner"), match.group("repo")


def issue_type(issue_data):
    value = issue_data.get("type")
    if isinstance(value, dict):
        return value.get("name")
    return value


def sub_issues(owner, repo, number):
    data = api("GET", f"/repos/{owner}/{repo}/issues/{number}/sub_issues", allow_errors=True)
    if not data:
        return []
    return [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
        }
        for item in data
    ]


def cmd_fetch(args):
    branch_name = branch()
    number = args.issue if args.issue is not None else branch_issue_number(branch_name)
    if number is None:
        raise SystemExit(
            "이슈 번호를 찾을 수 없습니다. 명령에 이슈 번호를 함께 넘기거나 "
            f"브랜치명에 #번호를 포함하세요. 현재 브랜치: {branch_name or '(detached)'}"
        )

    owner, repo = repo_slug()
    issue_data = api("GET", f"/repos/{owner}/{repo}/issues/{number}")

    data = {
        "branch": branch_name,
        "issue": number,
        "issue_from": "argument" if args.issue is not None else "branch",
        "repo": f"{owner}/{repo}",
        "title": issue_data.get("title"),
        "state": issue_data.get("state"),
        "type": issue_type(issue_data),
        "labels": [label["name"] for label in issue_data.get("labels", [])],
        "assignees": [user["login"] for user in issue_data.get("assignees", [])],
        "url": issue_data.get("html_url"),
        "body": issue_data.get("body") or "",
        "sub_issues": sub_issues(owner, repo, number),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="issue-work 스킬 헬퍼")
    sub = parser.add_subparsers(dest="cmd", required=True)

    fetch = sub.add_parser("fetch", help="이슈 내용 조회 (인자 없으면 브랜치명에서 번호 추출)")
    fetch.add_argument("--issue", type=int, default=None, help="작업할 이슈 번호. 생략하면 브랜치명의 #번호 사용")
    fetch.set_defaults(func=cmd_fetch)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
