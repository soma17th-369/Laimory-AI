# Pull Request 템플릿·관례

## Scope

`.github/pull_request_template.md`의 기존 PR 템플릿을 coding agent가 그대로 작성하도록 안내하고, PR에 올릴 commit을 작은 작업 단위로 나누는 현재 지침을 기록한다.

## Read When

- PR을 작성·수정·검토할 때
- `.github/pull_request_template.md`를 바꿀 때
- PR에 포함할 commit을 작업 단위로 나눌 때

## Authoritative Sources

- `.github/pull_request_template.md`
- `AGENTS.md`
- `conventions/branch.md`
- `conventions/commit.md`
- 실제 GitHub Pull Request와 merge 이력

## Current Implementation

아래는 `.github/pull_request_template.md`의 현재 내용이다. 실제 PR 작성 시에는 이 복사본보다 원본 파일을 우선한다.

```markdown
## 관련 이슈
<!-- 해결한 문제를 지정하는 Issue Index에 연결해야 합니다. -->

- Resolves : 

## 작업 사항
<!-- 해당 Pull Request에서 수행한 작업 목록을 제시해야 합니다. -->


## DB 변경 사항
<!-- 작업 사항이 DB에 영향이 있는 작업이라면 변경 사항을 적어야 합니다. -->

## 참고 사항
<!-- 기능을 만들기 위해 다른사람들이 참고해야할 사항을 적습니다. -->

## 변경된 API
<!-- 프론트엔드 개발자와 공유하기 위해 텔레그램을 통해 공유될 API 변동사항을 적습니다. ex) [API description](명세서 링크) -->
```

최근 PR은 일반적으로 `dev`를 base로 하고 `feat/#<issue>`, `fix/#<issue>`, `refactor/#<issue>` branch를 head로 사용한다. PR을 준비할 때 commit은 `conventions/commit.md`에 따라 하나의 주된 목적을 가진 작은 작업 단위로 최대한 세분화한다.

## Invariants

- PR 본문은 `.github/pull_request_template.md`의 현재 원본을 기준으로 작성한다.
- 템플릿에 없는 섹션을 현재 필수 계약처럼 임의로 추가하지 않는다.
- `Resolves` 대상은 실제로 PR이 해결하는 이슈와 일치시킨다.
- PR용 commit은 하나의 주된 목적을 갖고 독립적으로 검토·revert할 수 있게 작은 작업 단위로 나눈다.
- commit·push·PR은 사용자가 요청하거나 승인한 경우에만 수행한다.

## Known Gaps

- 현재 원본 PR 템플릿에는 테스트·검증 결과 전용 섹션이 없다.
- PR 제목·본문 형식을 검사하는 CI 설정은 저장소에서 확인하지 못했다.
- GitHub merge method 설정은 로컬 파일만으로 확정할 수 없다.

## Update When

`.github/pull_request_template.md`의 섹션·주석, PR base·head 관례, commit 분할·merge 방식이 실제로 바뀐 때 갱신한다.

## Validation

- `Get-Content .github/pull_request_template.md -Encoding utf8`
- `git branch --all --no-color`
- `git log --merges -20 --pretty=format:"%h %s"`
- GitHub API로 최근 PR의 title, base, head 확인
