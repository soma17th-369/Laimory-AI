# 브랜치 관례

## Scope

현재 local/remote branch와 merge history에서 확인되는 branch 이름·이슈 연결 관례를 기록한다. 저장소가 기술적으로 강제하지 않는 규칙은 관찰된 관례로만 표현한다.

## Read When

- 새 작업 branch를 만들 때
- branch 이름에서 issue와 작업 성격을 해석할 때
- PR base/head를 정할 때

## Authoritative Sources

- `git branch --all`, `.git/config`의 branch tracking
- 최근 `git log --merges`
- `.github/ISSUE_TEMPLATE/**`, `.github/pull_request_template.md`
- `deploy-ec2.yml`·`deploy-production.yml`의 deploy branch trigger
- `.github/workflows/pr-main-guard.yml`, `docs/github/main-ruleset.example.json`

## Current Implementation

remote HEAD와 local tracking 기준 기본 개발 branch는 `dev`다. `dev` push는 EC2(개발)로 자동 배포된다.

`main`은 production의 정본이다(#90). `main` push가 `deploy-production.yml`로 AgentCore Runtime에 배포하며, 일반 승격은 `dev`, 긴급 수정은 `hotfix`에서 온 PR만 받는다. `pr-main-guard.yml`이 source 저장소와 이 두 브랜치를 검사하고 ruleset의 required status check가 merge를 막는다. remote `prod` branch도 남아 있지만 어느 workflow도 참조하지 않는다.

promotion 흐름은 `feat|fix|refactor/#<issue>` → `dev` → `main`이다. 운영 장애를 즉시 복구할 때만 현재 `main`에서 분기한 공용 `hotfix` 브랜치를 사용해 `hotfix` → `main` PR을 연다.

최근 작업 branch는 작업 성격과 GitHub issue 번호를 결합한다.

- `feat/#<issue>`: 새 기능
- `fix/#<issue>`: bug fix
- `refactor/#<issue>`: 기존 구조·품질 개선
- `hotfix`: `main`에서 분기하는 공용 운영 긴급 수정 브랜치

필요하면 issue 번호 뒤에 짧은 slug를 붙인 사례(`fix/#47-filebeat-strict-perms`)가 있다. merge history는 `... from <owner>/<type>/#<issue>` 형태로 issue branch를 추적한다.

PR template는 해결 issue를 `Resolves`로 연결하고 작업 사항, DB 영향, 참고 사항, 변경 API를 기록하도록 요구한다.

## Invariants

- issue 기반 작업은 branch 이름에 `#<issue>`를 포함해 추적 가능하게 한다.
- prefix는 작업의 주된 목적과 맞춘다.
- `dev` push가 개발 배포를, `main` push가 production 배포를 일으킴을 인지하고 base·merge 시점을 선택한다.
- 일반 이슈 작업 branch의 PR base는 `dev`다. 운영 긴급 수정인 `hotfix`만 `main`을 base로 열 수 있다.
- branch를 만들기 전에 동일 issue branch가 local/remote에 이미 있는지 확인한다.

## Known Gaps

- branch naming을 검사하는 CI나 server-side rule은 저장소에 없다.
- `main` 보호(직접 push 차단·PR 필수·required check)는 GitHub ruleset 설정이라 로컬 파일만으로 확인할 수 없다. 적용 payload는 `docs/github/main-ruleset.example.json`에 있고 절차는 `docs/deploy-production.md`에 있다.
- 조사 시점 기준 remote에 `main`이 없다. 생성 절차는 `docs/deploy-production.md` §3.1이다.
- remote `prod` branch의 용도는 확인되지 않았다.
- `chore`, `docs`, `test` 전용 branch prefix의 충분한 사용 사례는 확인되지 않았다.

## Update When

실제 branch prefix, issue 연결 형식, 기본 PR base, deploy trigger branch 또는 promotion 흐름이 달라질 때 갱신한다.

## Validation

- `git branch --all --no-color`
- `git log --merges -30 --pretty=format:"%h %s"`
- `git remote show origin`은 network 접근이 허용되고 최신 remote 상태가 필요할 때만 사용

