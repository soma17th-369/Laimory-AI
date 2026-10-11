---
name: orca-issue-worktree
description: GitHub 이슈 번호와 실행 에이전트(claude 또는 codex)를 받아, 일반 이슈는 최신 origin/dev에서 이슈 브랜치로, HotFix는 최신 origin/main에서 공용 hotfix 브랜치로 연결된 Orca 워크트리를 생성한다. "이슈 워크트리 만들어줘", "Orca에 이슈 작업 공간 추가", "$orca-issue-worktree 123 claude" 같은 요청에 사용한다. 기존 워크트리에서 바로 코드를 구현하거나 일반 git worktree만 만드는 요청에는 사용하지 않는다.
---

# Orca 이슈 워크트리

특정 GitHub 이슈를 독립된 Orca 워크트리와 선택한 에이전트에 연결한다.

## 입력

- `issue`: 양의 GitHub 이슈 번호
- `agent`: `claude` 또는 `codex`

예: `$orca-issue-worktree 114 claude`

두 값 중 빠진 값만 사용자에게 묻는다. 이슈 제목이 저장소의 표준 Type prefix가 아니거나
`🔧 Task`라서 브랜치 prefix를 자동 결정할 수 없을 때만 `feat`, `fix`, `refactor` 중 주된 목적에
맞는 값을 추가로 확인한다. `🚑 HotFix`는 별도 확인 없이 저장소의 HotFix 흐름을 따른다.

## 실행

1. 변경 전에 `.agents/knowledge/conventions/branch.md`를 읽는다.
2. 주 워크트리의 기존 변경은 사용자 작업으로 보고 수정하거나 옮기지 않는다.
3. `gh auth status`로 GitHub 인증을 확인한다. Orca 앱은 헬퍼가 연결한다.
4. 실제 생성은 아래 헬퍼를 저장소 루트에서 실행한다. 이 명령은 GitHub 조회, 대상 origin branch
   갱신, Orca 워크트리 생성, 브랜치 이름 변경과 Claude/Codex 터미널 시작을 포함하므로 실행
   직전에 필요한 권한 승인을 요청한다.

```powershell
python .agents/skills/orca-issue-worktree/scripts/create_worktree.py --issue 114 --agent claude
```

표준 Type이 아닌 이슈에서 사용자가 branch prefix를 정한 경우에만 다음 옵션을 덧붙인다.

```powershell
python .agents/skills/orca-issue-worktree/scripts/create_worktree.py --issue 125 --agent codex --branch-prefix refactor
```

Claude 환경에서는 같은 내용의
`.claude/skills/orca-issue-worktree/scripts/create_worktree.py`를 사용한다.

## 결과 계약

헬퍼는 UTF-8 JSON 하나를 출력한다. 성공 시 다음 조건을 모두 확인해 사용자에게 보고한다.

- Orca 표시 이름이 GitHub 이슈 제목과 정확히 같다.
- `linkedIssue`가 요청한 번호다.
- `createdWithAgent`가 요청한 `claude` 또는 `codex`다.
- 일반 이슈는 최신 `origin/dev`에서 `feat|fix|refactor/#<issue>` 브랜치를 만든다.
- `🚑 HotFix`는 최신 `origin/main`에서 공용 `hotfix` 브랜치를 만든다.
- Orca workspace status는 `in-progress`다. GitHub Project 상태는 바꾸지 않는다.

같은 이슈의 Orca 워크트리나 같은 local/remote 브랜치가 이미 있으면 헬퍼는 덮어쓰기나 삭제 없이
중단한다. 공용 `hotfix`가 남아 있을 때도 자동으로 reset하거나 삭제하지 않는다. 생성 이후 단계에서
실패하면 JSON의 `partial`을 보고하고, 같은 명령을 맹목적으로 다시 실행하지 말고 남은 워크트리
상태를 먼저 확인한다.
