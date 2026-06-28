---
name: issue-pr
description: Create a commit and GitHub pull request from the current branch by extracting the issue number after # in branch names like feat/#33, fix/#25, or refactor/#10; use when the user asks to commit current work according to repository commit rules, inspect the linked GitHub issue, and open a PR.
---

# issue-pr

Use this skill to turn the current branch work into a rule-compliant commit and GitHub pull request.

## Rules

- Treat the number after `#` in the current branch name as the GitHub issue number.
- Expect work branches to be created from `dev` and named like `feat/#33`, `fix/#25`, or `refactor/#10`.
- Use the issue content to understand intent, scope, checklist, and PR wording.
- Do not commit unrelated user changes. Review changed files before staging.
- Do not run destructive git commands.
- If the branch has no `#<number>`, stop and ask for the issue number or correct branch.
- If GitHub CLI is unavailable or unauthenticated, report the blocker and give the exact command the user needs to run.

## Workflow

1. Inspect branch and issue.
   - Run `git branch --show-current`.
   - Extract the issue number with `#(\d+)`.
   - Run `gh issue view <number> --comments` to read the GitHub issue.
   - Determine the likely commit type from the branch prefix first: `feat`, `fix`, or `refactor`.
   - If branch prefix and issue template conflict, prefer the branch prefix and mention the mismatch.

2. Inspect work done so far.
   - Run `git status --short`.
   - Review relevant diffs with `git diff` and, when needed, staged diff with `git diff --cached`.
   - Read modified files enough to understand behavior.
   - Run focused tests or checks that match the touched area when feasible.
   - If changes include generated files, dependency lockfiles, or unrelated edits, separate them from the main commit or ask before including them.

3. Choose the commit type.
   - Use branch prefix when it is one of `feat`, `fix`, or `refactor`.
   - Otherwise infer from the actual change using the repository convention:
     - `feat`: Add new features
     - `fix`: Fix bugs
     - `docs`: Modify documentation
     - `style`: Code formatting only
     - `refactor`: Code refactoring
     - `test`: Add or refactor tests
     - `chore`: Package manager or miscellaneous changes
     - `design`: UI design or CSS changes
     - `comment`: Add or modify necessary comments
     - `rename`: Only file or folder renames
     - `remove`: Only file or folder deletions

4. Create the commit.
   - Stage only relevant files.
   - Use this exact commit message format:
     `type : concise Korean summary`
   - Keep the summary short and based on the issue plus actual diff.
   - Example: `feat : 로그인 API 추가`

5. Create the pull request.
   - Use `dev` as the base branch unless the user explicitly says otherwise.
   - Use the current branch as the head branch.
   - Push the branch if needed: `git push -u origin <current-branch>`.
   - Create the PR with `gh pr create --base dev --head <current-branch>`.
   - PR title should follow the same convention as the commit message.
   - PR body should include:
     - linked issue: `Closes #<number>`
     - concise summary of completed work
     - tests/checks run, or `Not run` with the reason
     - notes for DB changes, API changes, or follow-up risks when relevant

## Issue Templates

Use issue sections as signals:

- Bug issue: identify affected behavior, environment, API, error code/logs, and solution.
- Feature issue: identify planned feature, implementation details, references, DB changes, and checklist.
- Refactor issue: identify target feature, reason, and checklist.

## Output

Report:

- branch name and issue number
- commit hash and commit message
- PR URL
- tests/checks run
- any skipped or excluded changes
