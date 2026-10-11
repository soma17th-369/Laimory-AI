#!/usr/bin/env python3
"""GitHub 이슈에 연결된 Orca 워크트리를 안전하게 생성한다."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# .agents 또는 .claude 아래의 skills/orca-issue-worktree/scripts 기준 저장소 루트
ROOT = Path(__file__).resolve().parents[4]
STANDARD_PREFIXES = {
    "🐛 Bug - ": "fix",
    "✨ Feature - ": "feat",
    "🎨 Refactor - ": "refactor",
    "🚑 HotFix - ": "hotfix",
}
ALLOWED_AGENTS = ("claude", "codex")
ALLOWED_BRANCH_PREFIXES = ("feat", "fix", "refactor")


@dataclass
class WorkflowError(Exception):
    phase: str
    message: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class BranchPlan:
    branch: str
    base_branch: str
    base_ref: str


def run(command: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkflowError("preflight", f"명령을 찾을 수 없습니다: {command[0]}") from exc


def output_text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip()).strip()


def run_checked(command: list[str], *, phase: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    result = run(command, cwd=cwd)
    if result.returncode != 0:
        raise WorkflowError(phase, output_text(result) or f"명령 실행 실패: {command[0]}")
    return result


def parse_json(result: subprocess.CompletedProcess[str], *, phase: str) -> dict[str, Any]:
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(phase, "명령이 유효한 JSON을 반환하지 않았습니다.") from exc
    if not isinstance(data, dict):
        raise WorkflowError(phase, "명령의 JSON 응답이 객체가 아닙니다.")
    return data


def require_ok(data: dict[str, Any], *, phase: str) -> dict[str, Any]:
    if data.get("ok") is not True:
        error = data.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise WorkflowError(phase, message or "Orca 명령이 실패했습니다.", {"orca": data})
    result = data.get("result")
    if not isinstance(result, dict):
        raise WorkflowError(phase, "Orca 성공 응답에 result가 없습니다.")
    return result


def git_output(args: list[str], *, phase: str = "preflight") -> str:
    return run_checked(["git", *args], phase=phase).stdout.strip()


def repo_slug() -> str:
    remote = git_output(["remote", "get-url", "origin"])
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", remote)
    if not match:
        raise WorkflowError("preflight", f"origin에서 GitHub 저장소를 해석할 수 없습니다: {remote}")
    return f"{match.group('owner')}/{match.group('repo')}"


def branch_prefix(title: str, override: str | None = None) -> str:
    if override:
        return override
    for title_prefix, value in STANDARD_PREFIXES.items():
        if title.startswith(title_prefix):
            return value
    raise WorkflowError(
        "preflight",
        "이슈 제목에서 브랜치 prefix를 결정할 수 없습니다. --branch-prefix로 feat, fix, refactor 중 하나를 지정하세요.",
    )


def branch_plan(number: int, title: str, override: str | None = None) -> BranchPlan:
    prefix = branch_prefix(title, override)
    if prefix == "hotfix":
        return BranchPlan(
            branch="hotfix",
            base_branch="origin/main",
            base_ref="refs/remotes/origin/main",
        )
    return BranchPlan(
        branch=f"{prefix}/#{number}",
        base_branch="origin/dev",
        base_ref="refs/remotes/origin/dev",
    )


def branch_exists(ref: str) -> bool:
    result = run(["git", "show-ref", "--verify", "--quiet", ref])
    if result.returncode not in (0, 1):
        raise WorkflowError("preflight", output_text(result) or f"Git ref 확인 실패: {ref}")
    return result.returncode == 0


def orca_worktree(data: dict[str, Any], *, phase: str) -> dict[str, Any]:
    result = require_ok(data, phase=phase)
    worktree = result.get("worktree", result)
    if not isinstance(worktree, dict) or not worktree.get("path"):
        raise WorkflowError(phase, "Orca 응답에서 워크트리 경로를 찾을 수 없습니다.")
    return worktree


def worktree_summary(worktree: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "path",
        "branch",
        "displayName",
        "linkedIssue",
        "workspaceStatus",
        "createdWithAgent",
    )
    return {field: worktree.get(field) for field in fields}


def fetch_issue(number: int, repo: str) -> dict[str, Any]:
    result = run_checked(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,state,url",
        ],
        phase="issue",
    )
    data = parse_json(result, phase="issue")
    if data.get("state") != "OPEN":
        raise WorkflowError("issue", f"열린 이슈만 생성할 수 있습니다. 현재 상태: {data.get('state')}")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise WorkflowError("issue", "GitHub 이슈 제목이 비어 있습니다.")
    return data


def ensure_orca_ready_and_unique(number: int) -> None:
    opened = parse_json(run_checked(["orca", "open", "--json"], phase="orca-open"), phase="orca-open")
    require_ok(opened, phase="orca-open")

    shown_result = run(["orca", "worktree", "show", "--worktree", f"issue:{number}", "--json"])
    shown = parse_json(shown_result, phase="duplicate-check")
    if shown.get("ok") is True:
        existing = orca_worktree(shown, phase="duplicate-check")
        raise WorkflowError(
            "duplicate-check",
            f"이슈 #{number}에 연결된 Orca 워크트리가 이미 있습니다.",
            {"existing": worktree_summary(existing)},
        )
    error = shown.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if code != "selector_not_found":
        raise WorkflowError("duplicate-check", output_text(shown_result) or "Orca 중복 확인에 실패했습니다.")


def create_worktree(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    repo = repo_slug()
    issue = fetch_issue(args.issue, repo)
    plan = branch_plan(args.issue, issue["title"], args.branch_prefix)
    state.update(
        {
            "issue": args.issue,
            "title": issue["title"],
            "agent": args.agent,
            "branch": plan.branch,
            "baseRef": plan.base_ref,
        }
    )

    ensure_orca_ready_and_unique(args.issue)
    run_checked(["git", "fetch", "origin"], phase="fetch")

    local_ref = f"refs/heads/{plan.branch}"
    remote_ref = f"refs/remotes/origin/{plan.branch}"
    existing_refs = [ref for ref in (local_ref, remote_ref) if branch_exists(ref)]
    if existing_refs:
        raise WorkflowError(
            "duplicate-check",
            "같은 작업 브랜치가 이미 있어 새 워크트리를 만들지 않았습니다.",
            {"existingRefs": existing_refs},
        )
    if not branch_exists(plan.base_ref):
        raise WorkflowError("fetch", f"{plan.base_branch}을 찾을 수 없습니다.")

    create_command = [
        "orca",
        "worktree",
        "create",
        "--repo",
        f"path:{ROOT}",
        "--name",
        plan.branch,
        "--base-branch",
        plan.base_branch,
        "--issue",
        str(args.issue),
        "--agent",
        args.agent,
        "--setup",
        "inherit",
        "--no-parent",
        "--json",
    ]
    created_data = parse_json(run_checked(create_command, phase="create"), phase="create")
    created = orca_worktree(created_data, phase="create")
    path_text = created["path"]
    path = Path(path_text)
    state.update({"created": True, "path": str(path), "orcaId": created.get("id")})

    current_branch = run_checked(
        ["git", "branch", "--show-current"],
        phase="branch-rename",
        cwd=path,
    ).stdout.strip()
    if current_branch != plan.branch:
        run_checked(["git", "branch", "-m", plan.branch], phase="branch-rename", cwd=path)
    state["branchRenamed"] = True

    set_command = [
        "orca",
        "worktree",
        "set",
        "--worktree",
        f"path:{path_text}",
        "--display-name",
        issue["title"],
        "--workspace-status",
        "in-progress",
        "--json",
    ]
    set_data = parse_json(run_checked(set_command, phase="metadata"), phase="metadata")
    require_ok(set_data, phase="metadata")
    state["metadataSet"] = True

    final_data = parse_json(
        run_checked(
            ["orca", "worktree", "show", "--worktree", f"path:{path_text}", "--json"],
            phase="verify",
        ),
        phase="verify",
    )
    final = orca_worktree(final_data, phase="verify")
    expected = {
        "displayName": issue["title"],
        "linkedIssue": args.issue,
        "workspaceStatus": "in-progress",
        "createdWithAgent": args.agent,
        "branch": f"refs/heads/{plan.branch}",
        "baseRef": plan.base_ref,
    }
    mismatches = {
        key: {"expected": value, "actual": final.get(key)}
        for key, value in expected.items()
        if final.get(key) != value
    }
    if mismatches:
        raise WorkflowError("verify", "생성 결과가 요청한 계약과 다릅니다.", {"mismatches": mismatches})

    return {
        "ok": True,
        "issue": args.issue,
        "title": issue["title"],
        "url": issue.get("url"),
        "agent": args.agent,
        "status": final["workspaceStatus"],
        "branch": plan.branch,
        "baseRef": final["baseRef"],
        "path": final["path"],
        "worktreeId": final.get("id"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitHub 이슈용 Orca 워크트리를 생성합니다.")
    parser.add_argument("--issue", type=int, required=True, help="양의 GitHub 이슈 번호")
    parser.add_argument("--agent", required=True, choices=ALLOWED_AGENTS, help="시작할 Orca 에이전트")
    parser.add_argument(
        "--branch-prefix",
        choices=ALLOWED_BRANCH_PREFIXES,
        help="표준 Type 제목이 아닐 때 사용할 브랜치 prefix",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.issue <= 0:
        print(json.dumps({"ok": False, "phase": "input", "error": "--issue는 양수여야 합니다."}, ensure_ascii=False))
        return 2

    state: dict[str, Any] = {}
    try:
        result = create_worktree(args, state)
    except WorkflowError as exc:
        payload: dict[str, Any] = {"ok": False, "phase": exc.phase, "error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        if state.get("created"):
            payload["partial"] = state
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
