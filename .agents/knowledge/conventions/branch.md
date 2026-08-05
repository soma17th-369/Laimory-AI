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
- `deploy-ec2.yml`의 deploy branch trigger

## Current Implementation

remote HEAD와 local tracking 기준 기본 개발 branch는 `dev`다. EC2 자동 배포도 `dev` push를 trigger로 사용한다. `main`과 remote `prod`도 존재하지만 이 저장소 파일만으로 release promotion 규칙 전체를 확정할 수 없다.

최근 작업 branch는 작업 성격과 GitHub issue 번호를 결합한다.

- `feat/#<issue>`: 새 기능
- `fix/#<issue>`: bug fix
- `refactor/#<issue>`: 기존 구조·품질 개선

필요하면 issue 번호 뒤에 짧은 slug를 붙인 사례(`fix/#47-filebeat-strict-perms`)가 있다. merge history는 `... from <owner>/<type>/#<issue>` 형태로 issue branch를 추적한다.

PR template는 해결 issue를 `Resolves`로 연결하고 작업 사항, DB 영향, 참고 사항, 변경 API를 기록하도록 요구한다.

## Invariants

- issue 기반 작업은 branch 이름에 `#<issue>`를 포함해 추적 가능하게 한다.
- prefix는 작업의 주된 목적과 맞춘다.
- `dev` push가 자동 배포됨을 인지하고 base·merge 시점을 선택한다.
- branch를 만들기 전에 동일 issue branch가 local/remote에 이미 있는지 확인한다.

## Known Gaps

- branch naming을 검사하는 CI나 server-side rule은 저장소에 없다.
- `main`, `dev`, `prod` 사이의 전체 promotion·보호 branch 정책은 GitHub 설정 영역이라 로컬 파일만으로 확인할 수 없다.
- `chore`, `docs`, `test` 전용 branch prefix의 충분한 사용 사례는 확인되지 않았다.

## Update When

실제 branch prefix, issue 연결 형식, 기본 PR base, deploy trigger branch 또는 promotion 흐름이 달라질 때 갱신한다.

## Validation

- `git branch --all --no-color`
- `git log --merges -30 --pretty=format:"%h %s"`
- `git remote show origin`은 network 접근이 허용되고 최신 remote 상태가 필요할 때만 사용

