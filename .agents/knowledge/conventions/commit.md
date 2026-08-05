# 커밋·PR 관례

## Scope

현재 history에서 확인되는 commit message 형식과 PR을 읽기 쉽고 되돌리기 쉽게 만드는 commit 분할 원칙을 정의한다.

## Read When

- commit을 만들거나 기존 작업을 commit 단위로 나눌 때
- PR을 준비·검토하거나 rebase 순서를 정할 때
- commit message prefix와 설명을 고를 때

## Authoritative Sources

- 최근 non-merge·merge Git history
- `.github/pull_request_template.md`
- 실제 branch 이름과 issue 연결

## Current Implementation

최근 일반 commit은 `type : 한글 설명` 형식이 우세하다. 관찰되는 type은 다음과 같다.

- `feat`: 새 기능
- `fix`: 결함 수정
- `refactor`: 기존 동작·구조·품질 개선
- `test`: 테스트 추가·정비
- `docs`: 문서 변경
- `chore`: 도구·ignore·관리 작업

예시는 `refactor : App Server API 기반 데이터 경계 전환`, `fix : Filebeat strict.perms 옵션 오타 수정`처럼 type 뒤 공백, colon, 공백을 두고 변경 결과를 구체적으로 적는다. merge commit은 GitHub 기본 `Merge pull request #<PR> from ...` 형태가 관찰된다.

### PR용 commit 분할

PR에 올릴 때는 가능한 한 작업 단위를 작고 구체적으로 나눈다. 목표는 commit 하나만 읽어도 변경 이유와 영향 범위를 이해하고, 필요하면 그 commit만 안전하게 되돌릴 수 있게 하는 것이다.

- commit 하나에는 하나의 주된 목적만 둔다. unrelated refactor, formatting, 문서 정리를 섞지 않는다.
- 계약 변경, 내부 구현, 회귀 테스트, 운영·문서 갱신이 독립적으로 검토 가능하면 각각 별도 commit으로 나눈다.
- 큰 변경은 선행 구조 준비 → schema/contract → runtime 구현 → 회귀 테스트 보강 → 운영·문서 갱신처럼 의존 순서대로 쌓는다.
- 각 commit은 가능한 한 import·test가 깨지지 않는 완결 상태여야 한다. 코드와 필수 테스트를 억지로 분리해 중간 commit을 실패 상태로 만들지는 않는다.
- mechanical rename/formatting과 의미 변경을 분리해 diff에서 의미를 숨기지 않는다.
- generated/lock 변경은 원인을 만든 dependency 변경과 연결되게 두되, 별도 검토 가치가 있으면 명확한 commit으로 나눈다.
- commit message는 “수정”, “작업”처럼 모호하게 쓰지 않고 무엇의 어떤 계약·동작을 왜 바꿨는지 드러낸다.
- PR 본문은 commit 나열을 복사하는 대신 전체 목표, 주요 판단, 검증, API·DB 영향을 설명한다.

예시 분할:

1. `refactor : Timeline 결과 질문 저장 계약 추가`
2. `feat : 확정 이벤트 회고 질문 생성 단계 연결`
3. `test : Question Agent 실패와 결과 변환 회귀 검증`
4. `docs : Timeline 질문 계약과 Agent 흐름 갱신`

## Invariants

- 실제 commit을 만들기 전 `git diff`와 기존 사용자 변경을 확인하고 자기 변경만 stage한다.
- message type과 실제 주된 변경 목적을 일치시킨다.
- 작은 commit 원칙은 “파일 하나당 commit”이 아니다. 하나의 동작을 완결하는 code·test는 함께 둘 수 있다.
- commit·push·PR은 사용자 요청이나 승인 없이 수행하지 않는다.
- PR용 commit은 독립 검토·bisect·revert가 가능한 순서로 유지한다.

## Known Gaps

- commit message lint, required conventional commit, signed commit을 강제하는 설정은 없다.
- squash/merge/rebase 중 어떤 GitHub merge method가 필수인지는 저장소 파일에서 확인되지 않는다.
- 기존 history에는 prefix가 없는 commit도 있어 이 형식이 기술적으로 강제되지는 않는다.

## Update When

실제 message 형식, 허용 type, commit 분할·merge 정책, PR template 또는 자동 검사가 바뀔 때 갱신한다.

## Validation

- `git log --no-merges -40 --pretty=format:"%h %s"`
- `git log --merges -20 --pretty=format:"%h %s"`
- commit 전 `git status --short`, `git diff --cached --stat`, `git diff --cached`
- PR 전 각 commit의 목적과 테스트 가능 상태를 `git log --reverse --stat <base>..HEAD`로 검토

