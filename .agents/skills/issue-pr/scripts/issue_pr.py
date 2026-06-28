#!/usr/bin/env python3
"""issue-pr 스킬 헬퍼."""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = Path(__file__).resolve().parents[1] / "references" / "pull-request-template.md"


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


def api(method, path, payload=None):
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {github_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read().decode("utf-8")
            return json.loads(data) if data else None
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API 실패: HTTP {exc.code} {message}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"GitHub API 연결 실패: {exc.reason}") from exc


def graphql(query, variables=None, allow_errors=False):
    payload = {"query": query, "variables": variables or {}}
    result = api("POST", "/graphql", payload)
    if result.get("errors") and not allow_errors:
        raise SystemExit(f"GitHub GraphQL 실패: {json.dumps(result['errors'], ensure_ascii=False)}")
    return result


def branch():
    return run_git(["branch", "--show-current"])


def issue_number(branch_name):
    match = re.search(r"#(\d+)", branch_name)
    if not match:
        raise SystemExit(f"브랜치명에서 이슈 번호를 찾을 수 없습니다: {branch_name}")
    return int(match.group(1))


def commit_type(branch_name):
    prefix = branch_name.split("/", 1)[0]
    return prefix if prefix in {"feat", "fix", "refactor"} else ""


def repo_slug():
    remote = run_git(["remote", "get-url", "origin"])
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", remote)
    if not match:
        raise SystemExit(f"origin 원격에서 GitHub 저장소를 해석할 수 없습니다: {remote}")
    return match.group("owner"), match.group("repo")


def compare_range(base):
    return f"origin/{base}..HEAD"


def commits(base):
    output = run_git(["log", "--oneline", compare_range(base)])
    return [line for line in output.splitlines() if line.strip()]


def changed_files(base):
    output = run_git(["diff", "--stat", compare_range(base)])
    return [line for line in output.splitlines() if line.strip()]


def issue(owner, repo, number):
    return api("GET", f"/repos/{owner}/{repo}/issues/{number}")


def viewer_login():
    return api("GET", "/user")["login"]


def open_pr(owner, repo, head_owner, branch_name, base):
    encoded_head = urllib.parse.quote(f"{head_owner}:{branch_name}", safe="")
    encoded_base = urllib.parse.quote(base, safe="")
    prs = api("GET", f"/repos/{owner}/{repo}/pulls?head={encoded_head}&base={encoded_base}&state=open")
    return prs[0] if prs else None


def issue_labels(issue_data):
    return [label["name"] for label in issue_data.get("labels", [])]


def issue_project_ids(owner, repo, number):
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          projectItems(first: 20) {
            nodes {
              project {
                id
                title
              }
            }
          }
        }
      }
    }
    """
    result = graphql(query, {"owner": owner, "repo": repo, "number": number}, allow_errors=True)
    if result.get("errors"):
        return [], result["errors"]
    nodes = result["data"]["repository"]["issue"]["projectItems"]["nodes"]
    return [{"id": node["project"]["id"], "title": node["project"]["title"]} for node in nodes], []


def add_to_project(project_id, content_id):
    mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item {
          id
        }
      }
    }
    """
    return graphql(mutation, {"projectId": project_id, "contentId": content_id}, allow_errors=True)


def pr_title(issue_title, extra):
    if not extra:
        return issue_title
    return f"{issue_title} : {extra}"


def update_issue_metadata(owner, repo, pr_number, assignee, labels):
    api("PATCH", f"/repos/{owner}/{repo}/issues/{pr_number}", {"assignees": [assignee]})
    if labels:
        api("POST", f"/repos/{owner}/{repo}/issues/{pr_number}/labels", {"labels": labels})


def copy_issue_projects_to_pr(owner, repo, issue_number_value, pr_node_id):
    copied = []
    projects, errors = issue_project_ids(owner, repo, issue_number_value)
    if errors:
        return copied, errors
    for project in projects:
        result = add_to_project(project["id"], pr_node_id)
        if not result.get("errors"):
            copied.append(project["title"])
    return copied, []


def default_work_items(base):
    items = []
    for line in commits(base):
        summary = re.sub(r"^[0-9a-f]+\s+", "", line).strip()
        if summary:
            items.append(f"- {summary}")
    return "\n".join(items) if items else "- 변경 사항 없음"


def multiline_items(value, fallback):
    if not value:
        return fallback
    items = []
    for raw in value.split("|"):
        text = raw.strip()
        if text:
            items.append(text if text.startswith("- ") else f"- {text}")
    return "\n".join(items) if items else fallback


def render_body(args):
    branch_name = branch()
    number = issue_number(branch_name)
    notes = [f"- 테스트/체크: {args.checks}" if args.checks else "- 테스트/체크: Not run"]
    if args.excluded:
        notes.append(f"- 제외한 변경: {args.excluded}")
    notes.append(
        f"- PR 본문 작성 기준: `git log --oneline {compare_range(args.base)}`, "
        f"`git diff --stat {compare_range(args.base)}`"
    )
    template = TEMPLATE.read_text(encoding="utf-8")
    return template.format(
        issue_ref=f"#{number}",
        work_items=multiline_items(args.summary, default_work_items(args.base)),
        db_changes=args.db_changes or "없음",
        notes="\n".join(notes),
        api_changes=args.api_changes or "없음",
    )


def cmd_inspect(args):
    branch_name = branch()
    number = issue_number(branch_name)
    owner, repo = repo_slug()
    issue_data = {} if args.local else issue(owner, repo, number)
    data = {
        "branch": branch_name,
        "issue": number,
        "commit_type": commit_type(branch_name),
        "repo": f"{owner}/{repo}",
        "issue_title": issue_data.get("title"),
        "issue_state": issue_data.get("state"),
        "commits": commits(args.base),
        "changed_files": changed_files(args.base),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_render(args):
    print(render_body(args))


def cmd_upsert(args):
    branch_name = branch()
    number = issue_number(branch_name)
    owner, repo = repo_slug()
    issue_data = issue(owner, repo, number)
    pr_body = render_body(args)
    issue_title = issue_data.get("title") or f"이슈 #{number} 처리"
    title = args.title or pr_title(issue_title, args.title_extra)

    existing = open_pr(owner, repo, owner, branch_name, args.base)
    payload = {"title": title, "body": pr_body}
    if existing:
        pr = api("PATCH", f"/repos/{owner}/{repo}/pulls/{existing['number']}", payload)
    else:
        payload.update({"head": branch_name, "base": args.base})
        pr = api("POST", f"/repos/{owner}/{repo}/pulls", payload)
    assignee = viewer_login()
    labels = issue_labels(issue_data)
    update_issue_metadata(owner, repo, pr["number"], assignee, labels)
    projects, project_errors = copy_issue_projects_to_pr(owner, repo, number, pr["node_id"])
    print(json.dumps({
        "number": pr["number"],
        "url": pr["html_url"],
        "title": pr["title"],
        "assignee": assignee,
        "labels": labels,
        "projects": projects,
        "project_errors": project_errors,
    }, ensure_ascii=False))


def cmd_finish(args):
    cmd_upsert(args)


def add_body_args(subparser):
    subparser.add_argument("--base", default="dev")
    subparser.add_argument("--summary", help="작업 사항. 여러 항목은 | 로 구분")
    subparser.add_argument("--checks", default="")
    subparser.add_argument("--excluded", default="")
    subparser.add_argument("--db-changes", default="없음")
    subparser.add_argument("--api-changes", default="없음")


def build_parser():
    parser = argparse.ArgumentParser(description="issue-pr 스킬 헬퍼")
    sub = parser.add_subparsers(dest="cmd", required=True)

    inspect = sub.add_parser("inspect", help="브랜치, 이슈, 전체 변경 범위 확인")
    inspect.add_argument("--base", default="dev")
    inspect.add_argument("--local", action="store_true", help="GitHub API 호출 없이 로컬 Git 정보만 확인")
    inspect.set_defaults(func=cmd_inspect)

    render = sub.add_parser("render", help="PR 템플릿 기반 본문 출력")
    add_body_args(render)
    render.set_defaults(func=cmd_render)

    upsert = sub.add_parser("upsert", help="열린 PR 갱신 또는 새 PR 생성")
    add_body_args(upsert)
    upsert.add_argument("--title", help="PR 제목을 직접 지정. 생략하면 이슈 제목을 그대로 사용")
    upsert.add_argument("--title-extra", help="이슈 제목 뒤에 ': 추가작업' 형식으로 붙일 세부 작업")
    upsert.set_defaults(func=cmd_upsert)

    finish = sub.add_parser("finish", help="GitHub API 호출을 한 번으로 모아 열린 PR 갱신 또는 새 PR 생성")
    add_body_args(finish)
    finish.add_argument("--title", help="PR 제목을 직접 지정. 생략하면 이슈 제목을 그대로 사용")
    finish.add_argument("--title-extra", help="이슈 제목 뒤에 ': 추가작업' 형식으로 붙일 세부 작업")
    finish.set_defaults(func=cmd_finish)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
