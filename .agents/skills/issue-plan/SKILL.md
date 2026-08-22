---
name: issue-plan
description: GitHub 이슈를 조회하고 실제 코드를 분석한 뒤 `.agents/plans/`에 실행계획을 저장해 사용자 승인을 받으며, 승인 후 구현·검증하고 `.agents/worklog/`에 결과를 기록한다. 이슈 번호가 주어지면 해당 이슈를, 없으면 현재 브랜치명의 `#번호` 이슈를 사용한다. "이 이슈 작업하자", "이슈 진행해줘", "이슈 23 작업 시작", "이 브랜치 이슈 처리하자" 같은 요청에 사용한다.
---

# issue-plan

GitHub 이슈를 `plan-work` 규약에 연결해 계획 → 승인 → 구현 → worklog 순서로 처리한다.

## 핵심 규칙

- 이슈 번호는 명령 인자, 현재 브랜치명의 `#번호` 순서로 결정한다. 둘 다 없으면 번호를 요청한다.
- 이슈 본문과 관련 코드를 끝까지 읽은 뒤 계획을 작성한다.
- 계획은 `.agents/plans/`의 Markdown 파일이 정본이다. 채팅에만 계획을 남기지 않는다.
- 계획·worklog는 `plan-work`의 「저장 위치」에 따라 **주 워크트리** 아래에 모은다.
  worktree 안에 만들면 계획이 흩어지고 git으로 추적되지 않는다.
- 계획은 두괄식으로 쓴다. 승인에 필요한 것(요약·실행 단계·결정·검증)이 먼저다.
- 사용자의 명시적 승인 전에는 제품 코드·설정·문서를 수정하지 않는다.
- 승인 뒤에는 계획 전체를 진행한다. 각 단계마다 추가 승인을 요구하지 않되 실질적인 계획 변경은 재승인받는다.
- 완료 후 계획 상태와 체크리스트를 갱신하고 `.agents/worklog/`에 결과를 기록한다.
- 이 스킬은 코드 작업과 검증까지만 수행한다. 커밋·push·PR은 `$issue-pr`로 넘긴다.
- 토큰과 비밀값을 출력하거나 계획·worklog에 기록하지 않는다.

## 1. 이슈 가져오기

```powershell
# 이슈 번호를 명시한 경우
python .agents/skills/issue-plan/scripts/issue_plan.py fetch --issue 23

# 현재 브랜치명의 #번호 사용
python .agents/skills/issue-plan/scripts/issue_plan.py fetch
```

출력의 `issue`, `title`, `state`, `type`, `labels`, `body`, `sub_issues`를 확인한다. 헬퍼는
`GITHUB_TOKEN` 환경 변수, `.env`, `.env.local` 순서로 토큰을 찾으며 값을 출력하지 않는다.

## 2. 이슈와 코드 이해하기

- 본문의 요구사항, 완료 조건, 체크리스트와 제외 범위를 정리한다.
- Bug는 재현과 영향 범위, Feature는 계약과 사용자 동작, Task는 대상과 보존할 동작을 우선 확인한다.
- 하위 이슈가 있으면 현재 작업에 포함할 범위를 사용자와 정한다.
- 관련 코드·문서·테스트와 git 상태를 읽어 현재 구조와 기존 변경을 파악한다.

## 3. 계획 파일 저장하고 승인받기

- `plan-work`의 [계획 템플릿](../plan-work/references/plan-template.md)을 순서 그대로 사용한다.
- 파일 경로는 `<주 워크트리>/.agents/plans/YYYY-MM-DD-issue-N-짧은-슬러그.md`로 정한다.
  주 워크트리는 `git rev-parse --path-format=absolute --git-common-dir` 출력의 부모 디렉터리다.
- frontmatter의 `issue`에 번호를 기록하고 최초 상태는 `draft`로 둔다.
- 계획 파일 링크와 핵심 단계·결정 사항을 사용자에게 보여주고 명시적 승인을 기다린다.
- 수정 요청이 있으면 계획을 갱신한 뒤 다시 확인받는다.

## 4. 승인된 계획 실행하기

- 승인 기록을 남기고 상태를 `approved`, 실제 착수 시 `in_progress`로 바꾼다.
- 계획 체크박스를 갱신하며 구현과 검증을 진행한다.
- 범위·공개 계약·위험 수준이 바뀌면 작업을 멈추고 계획을 수정해 재승인받는다.
- 기존 사용자 변경을 되돌리거나 계획 밖 리팩터링을 하지 않는다.

## 5. 완료 기록 남기기

- 완료 조건과 검증 결과를 확인하고 계획을 `completed`로 바꾼다.
- `plan-work`의 [worklog 템플릿](../plan-work/references/worklog-template.md)을 사용해
  `<주 워크트리>/.agents/worklog/YYYY-MM-DD-issue-N-같은-슬러그.md`를 작성한다.
- 계획 대비 실제 변경, 주요 판단, 검증, 변경 파일, 남은 일을 기록한다.
- 사용자에게 이슈 번호·변경 요약·검증 결과·계획 및 worklog 경로를 보고한다.

## 헬퍼

[scripts/issue_plan.py](scripts/issue_plan.py)는 이슈 번호 결정, 원격 저장소 해석, GitHub 이슈와
하위 이슈 조회, UTF-8 JSON 출력을 담당한다. 계획 수립과 구현 판단은 에이전트가 담당한다.
