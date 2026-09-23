from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "orca-issue-worktree"
    / "scripts"
    / "create_worktree.py"
)
SPEC = importlib.util.spec_from_file_location("orca_issue_worktree", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("🐛 Bug - 오류 수정", "fix"),
        ("✨ Feature - 기능 추가", "feat"),
        ("🎨 Refactor - 구조 개선", "refactor"),
        ("🚑 HotFix - 운영 장애 복구", "hotfix"),
    ],
)
def test_branch_prefix_follows_issue_title(title: str, expected: str) -> None:
    assert MODULE.branch_prefix(title) == expected


@pytest.mark.parametrize(
    ("number", "title", "expected_branch", "expected_base_branch", "expected_base_ref"),
    [
        (123, "🎨 Refactor - 구조 개선", "refactor/#123", "origin/dev", "refs/remotes/origin/dev"),
        (127, "🚑 HotFix - 운영 장애 복구", "hotfix", "origin/main", "refs/remotes/origin/main"),
    ],
)
def test_branch_plan_selects_branch_and_base_ref(
    number: int,
    title: str,
    expected_branch: str,
    expected_base_branch: str,
    expected_base_ref: str,
) -> None:
    plan = MODULE.branch_plan(number, title)

    assert plan.branch == expected_branch
    assert plan.base_branch == expected_base_branch
    assert plan.base_ref == expected_base_ref


def test_branch_prefix_uses_explicit_override_for_task() -> None:
    plan = MODULE.branch_plan(125, "🔧 Task - 운영 설정", "refactor")

    assert plan.branch == "refactor/#125"
    assert plan.base_branch == "origin/dev"
    assert plan.base_ref == "refs/remotes/origin/dev"


def test_branch_prefix_rejects_unknown_title_without_override() -> None:
    with pytest.raises(MODULE.WorkflowError) as exc_info:
        MODULE.branch_prefix("제목 관례가 없는 이슈")

    assert exc_info.value.phase == "preflight"


def test_worktree_summary_excludes_unneeded_orca_metadata() -> None:
    summary = MODULE.worktree_summary(
        {
            "id": "worktree-id",
            "path": "C:/workspace",
            "branch": "refs/heads/refactor/#123",
            "displayName": "이슈 제목",
            "linkedIssue": 123,
            "workspaceStatus": "in-progress",
            "createdWithAgent": "claude",
            "terminalSecret": "출력하면 안 되는 값",
        }
    )

    assert summary["linkedIssue"] == 123
    assert "terminalSecret" not in summary
