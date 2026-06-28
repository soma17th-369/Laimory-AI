#!/usr/bin/env python3
"""
gh_issue.py — create-issue 스킬의 메커니즘 헬퍼.

판단(Type/Priority/Size 결정, 모호하면 사람에게 묻기)은 SKILL.md를 따르는
Claude가 담당하고, 이 스크립트는 결정된 "이름"(예: Feature, High, M)을 받아
실제 GitHub API 호출(ID 해석 → 생성 → 필드 설정 → sub-issue 연결)만 수행한다.

옵션/타입 ID는 org admin이 언제든 바꿀 수 있으므로 하드코딩하지 않고
'이름'으로 매 실행마다 동적으로 해석한다. 이름이 안정적인 인터페이스다.

사용 예:
  # 단일 이슈 생성 + Type/Priority(+Size) 설정
  python gh_issue.py create --title "로그인 구현" --body-file body.md \
      --type Feature --priority High --size M

  # Epic의 하위 이슈로 붙이며 생성
  python gh_issue.py create --title "JWT 토큰 발급" --body-file t.md \
      --type Task --priority High --parent 12

  # 이미 있는 두 이슈를 부모-자식으로 연결
  python gh_issue.py link --parent 12 --child 13

출력은 항상 JSON 한 줄: {"number":..,"url":..,"id":..}
"""
import argparse
import json
import subprocess
import sys
import tempfile
import os

# Windows 한국어 로캘에서 한글 제목을 출력해도 깨지지 않도록 stdout/stderr을 UTF-8로 고정.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── 이 팀의 기본값 (다른 repo/project에서 쓰려면 --repo / --project 로 override) ──
# org는 팀 고정값. 하지만 org 안에 레포가 여러 개라 repo는 기본값을 두지 않는다 —
# 미지정이면 스킬(SKILL.md)이 `repos` 목록으로 사용자에게 어느 레포에 만들지 묻는다.
DEFAULT_ORG = os.environ.get("CREATE_ISSUE_ORG", "soma17th-369")
# 프로젝트도 기본값을 두지 않는다 — 미지정이면 스킬이 사용자에게 어느 프로젝트에 넣을지 묻고,
# 그 결과를 --project 로 넘긴다. (환경변수로만 선택적 기본값 지정 가능)
DEFAULT_PROJECT = os.environ.get("CREATE_ISSUE_PROJECT")


def gh(args, stdin=None):
    # gh 출력/입력은 UTF-8. Windows 한국어 로캘 기본값(cp949)이면 한글 디코딩이 깨지므로 강제 지정.
    r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", input=stdin)
    if r.returncode != 0:
        raise SystemExit(f"[gh 실패] gh {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def graphql(query):
    return json.loads(gh(["api", "graphql", "-f", f"query={query}"]))


def q(s):
    """문자열을 GraphQL 리터럴로 안전하게 인라인 (따옴표 이스케이프)."""
    return json.dumps(s)


# ── ID 해석 (이름 → ID) ──────────────────────────────────────────────
def org_of(repo):
    return repo.split("/")[0]


def norm_repo(repo):
    # repo가 'owner/name'이면 그대로, 'name'만 주면 org를 붙인다. 없으면 안내.
    if not repo:
        raise SystemExit("[인자 오류] --repo 가 필요하다 (org의 어느 레포에 만들지). "
                         "`repos` 서브커맨드로 목록을 받아 사용자에게 물어라.")
    return repo if "/" in repo else f"{DEFAULT_ORG}/{repo}"


def resolve_issue_type(org, name):
    d = graphql(f'{{ organization(login:{q(org)}){{ issueTypes(first:50){{ nodes{{ id name }} }} }} }}')
    for n in d["data"]["organization"]["issueTypes"]["nodes"]:
        if n["name"].lower() == name.lower():
            return n["id"]
    raise SystemExit(f"[해석 실패] Issue Type '{name}' 없음. 사용 가능: "
                     + ", ".join(n["name"] for n in d["data"]["organization"]["issueTypes"]["nodes"]))


def resolve_priority(org, option_name):
    d = graphql(
        f'{{ organization(login:{q(org)}){{ issueFields(first:50){{ nodes{{ __typename '
        f'... on IssueFieldSingleSelect {{ id name options{{ id name }} }} }} }} }} }}')
    for n in d["data"]["organization"]["issueFields"]["nodes"]:
        if n.get("name") == "Priority":
            for o in n["options"]:
                if o["name"].lower() == option_name.lower():
                    return n["id"], o["id"]
            raise SystemExit(f"[해석 실패] Priority 옵션 '{option_name}' 없음. 사용 가능: "
                             + ", ".join(o["name"] for o in n["options"]))
    raise SystemExit("[해석 실패] org에 Priority issue-field 없음")


def resolve_project(org, proj):
    d = graphql(f'{{ organization(login:{q(org)}){{ projectsV2(first:50){{ nodes{{ id number title }} }} }} }}')
    nodes = d["data"]["organization"]["projectsV2"]["nodes"]
    for n in nodes:
        if str(n["number"]) == str(proj) or n["title"] == proj:
            return n["id"]
    raise SystemExit(f"[해석 실패] 프로젝트 '{proj}' 없음. 사용 가능: "
                     + ", ".join(f'{n["number"]}:{n["title"]}' for n in nodes))


def resolve_size(project_id, option_name):
    d = graphql(
        f'{{ node(id:{q(project_id)}){{ ... on ProjectV2 {{ fields(first:50){{ nodes{{ '
        f'... on ProjectV2SingleSelectField {{ id name options{{ id name }} }} }} }} }} }} }}')
    for n in d["data"]["node"]["fields"]["nodes"]:
        if n.get("name") == "Size":
            for o in n["options"]:
                if o["name"].lower() == option_name.lower():
                    return n["id"], o["id"]
            raise SystemExit(f"[해석 실패] Size 옵션 '{option_name}' 없음. 사용 가능: "
                             + ", ".join(o["name"] for o in n["options"]))
    raise SystemExit("[해석 실패] 프로젝트에 Size 필드 없음")


def issue_node_id(repo, number):
    owner, name = repo.split("/")
    d = graphql(f'{{ repository(owner:{q(owner)},name:{q(name)}){{ issue(number:{number}){{ id }} }} }}')
    iid = d["data"]["repository"]["issue"]["id"]
    if not iid:
        raise SystemExit(f"[해석 실패] {repo}#{number} 이슈 없음")
    return iid


# ── 변경 작업 ────────────────────────────────────────────────────────
def create_issue(repo, title, body_file):
    url = gh(["issue", "create", "--repo", repo, "--title", title, "--body-file", body_file]).strip().splitlines()[-1]
    number = url.rstrip("/").rsplit("/", 1)[-1]
    return number, url, issue_node_id(repo, number)


def set_type(issue_id, type_id):
    graphql(f'mutation{{ updateIssue(input:{{ id:{q(issue_id)}, issueTypeId:{q(type_id)} }}){{ issue{{ number }} }} }}')


def set_priority(issue_id, field_id, option_id):
    graphql(f'mutation{{ setIssueFieldValue(input:{{ issueId:{q(issue_id)}, '
            f'issueFields:[{{ fieldId:{q(field_id)}, singleSelectOptionId:{q(option_id)} }}] }}){{ issue{{ number }} }} }}')


def add_to_project(project_id, issue_id):
    d = graphql(f'mutation{{ addProjectV2ItemById(input:{{ projectId:{q(project_id)}, contentId:{q(issue_id)} }}){{ item{{ id }} }} }}')
    return d["data"]["addProjectV2ItemById"]["item"]["id"]


def set_size(project_id, item_id, field_id, option_id):
    graphql(f'mutation{{ updateProjectV2ItemFieldValue(input:{{ projectId:{q(project_id)}, '
            f'itemId:{q(item_id)}, fieldId:{q(field_id)}, value:{{ singleSelectOptionId:{q(option_id)} }} }}){{ projectV2Item{{ id }} }} }}')


def link_sub(parent_id, child_id):
    graphql(f'mutation{{ addSubIssue(input:{{ issueId:{q(parent_id)}, subIssueId:{q(child_id)} }}){{ issue{{ number }} }} }}')


# ── CLI ──────────────────────────────────────────────────────────────
def cmd_create(a):
    a.repo = norm_repo(a.repo)
    org = org_of(a.repo)
    type_id = resolve_issue_type(org, a.type)
    pr_field, pr_opt = resolve_priority(org, a.priority)

    if a.size and not a.project:
        raise SystemExit("[인자 오류] --size 는 --project 와 함께 써야 한다 (Size는 프로젝트 필드).")

    # 프로젝트가 지정되면 Size 유무와 무관하게 이슈를 그 프로젝트에 추가한다.
    project_id = resolve_project(org, a.project) if a.project else None
    size_ids = resolve_size(project_id, a.size) if a.size else None  # (field_id, option_id)

    parent_id = issue_node_id(a.repo, a.parent) if a.parent else None

    number, url, issue_id = create_issue(a.repo, a.title, a.body_file)
    set_type(issue_id, type_id)
    set_priority(issue_id, pr_field, pr_opt)
    if project_id:
        item_id = add_to_project(project_id, issue_id)
        if size_ids:
            set_size(project_id, item_id, size_ids[0], size_ids[1])
    if parent_id:
        link_sub(parent_id, issue_id)

    print(json.dumps({"number": int(number), "url": url, "id": issue_id,
                      "type": a.type, "priority": a.priority,
                      "size": a.size, "project": a.project, "parent": a.parent}, ensure_ascii=False))


def cmd_projects(a):
    d = graphql(f'{{ organization(login:{q(DEFAULT_ORG)}){{ projectsV2(first:50){{ nodes{{ number title }} }} }} }}')
    print(json.dumps(d["data"]["organization"]["projectsV2"]["nodes"], ensure_ascii=False))


def cmd_repos(a):
    d = graphql(f'{{ organization(login:{q(DEFAULT_ORG)}){{ repositories(first:100, '
                f'orderBy:{{field:PUSHED_AT, direction:DESC}}){{ nodes{{ name }} }} }} }}')
    print(json.dumps([n["name"] for n in d["data"]["organization"]["repositories"]["nodes"]], ensure_ascii=False))


def cmd_link(a):
    a.repo = norm_repo(a.repo)
    parent_id = issue_node_id(a.repo, a.parent)
    child_id = issue_node_id(a.repo, a.child)
    link_sub(parent_id, child_id)
    print(json.dumps({"parent": a.parent, "child": a.child, "linked": True}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="create-issue 스킬 헬퍼")
    sub = p.add_subparsers(dest="cmd", required=True)

    repo_help = "대상 레포. 'owner/name' 또는 org 내 'name'만. 미지정이면 안내 후 중단. 목록은 `repos`."

    c = sub.add_parser("create", help="이슈 생성 + Type/Priority(+Size, +parent) 설정")
    c.add_argument("--repo", default=None, help=repo_help)
    c.add_argument("--title", required=True)
    c.add_argument("--body-file", required=True, help="이슈 본문 마크다운 파일 경로")
    c.add_argument("--type", required=True, help="Bug | Epic | Feature | Task")
    c.add_argument("--priority", required=True, help="Critical | High | Medium | Low | Backlog")
    c.add_argument("--size", help="XS | S | M | L | XL (생략 가능, 지정 시 --project 필요)")
    c.add_argument("--project", default=DEFAULT_PROJECT,
                   help="이슈를 추가할 프로젝트(번호 또는 제목). 미지정이면 어떤 프로젝트에도 추가하지 않는다.")
    c.add_argument("--parent", type=int, help="부모 이슈 번호 (sub-issue로 연결)")
    c.set_defaults(func=cmd_create)

    l = sub.add_parser("link", help="기존 두 이슈를 부모-자식으로 연결")
    l.add_argument("--repo", default=None, help=repo_help)
    l.add_argument("--parent", type=int, required=True)
    l.add_argument("--child", type=int, required=True)
    l.set_defaults(func=cmd_link)

    pj = sub.add_parser("projects", help="org의 프로젝트 목록(JSON: number/title) 출력")
    pj.set_defaults(func=cmd_projects)

    rp = sub.add_parser("repos", help="org의 레포 목록(JSON: name) 출력")
    rp.set_defaults(func=cmd_repos)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
