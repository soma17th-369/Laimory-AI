---
name: issue-pr
description: 현재 브랜치명의 #번호를 GitHub 이슈 번호로 사용해 변경 사항을 검토하고, 규칙에 맞는 커밋과 Pull Request를 생성하거나 갱신합니다. 사용자가 현재 작업을 커밋하고 PR로 올리라고 요청할 때 사용합니다.
---

# issue-pr

현재 브랜치 작업을 이슈와 연결된 커밋 및 Pull Request로 정리한다.

## 핵심 규칙

- `$issue-pr`가 명시적으로 호출되었을 때만 커밋, push, PR 생성/갱신을 수행한다.
- 일반적인 파일 수정, 스킬 개선, 코드 변경 요청을 처리하는 중에는 자동으로 커밋하거나 PR을 갱신하지 않는다.
- 사용자가 최종적으로 `$issue-pr`를 호출하면 그때까지 쌓인 변경을 모아서 처리한다.
- 현재 브랜치명에서 `#<번호>`를 추출해 GitHub 이슈 번호로 사용한다.
- 기본 base 브랜치는 `dev`이며, 비교 범위는 `origin/dev..HEAD`다.
- 커밋 메시지와 PR 제목은 `type : 한글 요약` 형식으로 작성한다.
- PR 제목은 기본적으로 연결된 GitHub 이슈 제목을 그대로 사용한다.
- 세부 작업을 추가로 명시해야 하면 `이슈 제목 : 추가작업` 형식으로 작성한다.
- PR assignee는 항상 현재 GitHub token 사용자로 지정한다.
- PR labels는 연결된 GitHub 이슈에 이미 지정된 labels를 그대로 사용한다.
- PR projects는 연결된 GitHub 이슈가 들어 있는 Projects V2를 그대로 사용한다.
- Projects V2 조회/추가는 token 권한이 부족할 수 있으므로, 실패하면 PR 갱신은 유지하고 권한 오류를 결과에 보고한다.
- PR 본문은 마지막 커밋 하나가 아니라 base 브랜치부터 현재 HEAD까지의 전체 누적 변경을 기준으로 작성한다.
- 사용자에게 보이는 커밋 메시지, PR 제목, PR 본문, 결과 보고는 한글로 작성한다.
- 관련 없는 staged 변경, 생성물, 캐시, 사용자가 만든 변경은 커밋에 섞지 않는다.
- 사용자가 명시하지 않은 파일 삭제, `git reset`, `git checkout` 같은 파괴적 작업은 하지 않는다.
- 토큰은 절대 출력, 커밋, PR 본문, 오류 보고에 포함하지 않는다.

## 남은 diff 처리 원칙

- `$issue-pr`가 호출되면 현재 작업트리에 남아 있는 code diff를 최대한 커밋으로 정리하는 것을 기본 목표로 한다.
- 변경이 생길 때마다 즉시 커밋하지 않는다. 관련 작업이 끝났거나 사용자가 `$issue-pr`를 호출한 시점에 한 번에 정리한다.
- 작은 변경 여러 개는 의미 있는 단위로 묶어 커밋 수를 줄인다.
- `git status --short`, `git diff`, `git diff --cached`를 기준으로 staged/unstaged/untracked 변경을 모두 확인한다.
- 이슈 범위와 명확히 관련 있는 변경은 가능한 한 포함한다.
- 관련 여부가 애매하지만 사용자가 "남은 diff 전부 처리", "승인한 내용"처럼 명시했으면 생성물까지 포함해 모두 커밋 대상으로 본다.
- 명백한 비밀 값, 로컬 환경 파일, 의존성 캐시처럼 저장소에 들어가면 안 되는 파일은 제외하고 이유를 보고한다.
- 여러 성격의 변경이 섞여 있으면 의미 있는 단위로 커밋을 나누되, 작은 잔여 diff는 하나의 정리 커밋으로 묶어도 된다.
- 이미 staged 된 삭제나 추가도 사용자 승인 변경으로 간주하되, diff를 읽고 깨진 문서/명백한 실수는 커밋 전에 바로잡는다.

## 참고 자료와 스크립트

- PR 본문 템플릿: [references/pull-request-template.md](references/pull-request-template.md)
- PR 자동화 헬퍼: [scripts/issue_pr.py](scripts/issue_pr.py)

스크립트는 다음을 보장한다.

- 현재 브랜치명에서 이슈 번호 추출
- 원격 저장소 owner/repo 확인
- `GITHUB_TOKEN` 환경 변수 또는 `.env.local` 토큰 사용
- GitHub 이슈 조회
- `origin/dev..HEAD` 기준 커밋 목록과 파일 변경 통계 수집
- PR 템플릿 기반 본문 렌더링
- 열린 PR이 있으면 갱신하고, 없으면 생성
- PR 제목을 이슈 제목 기준으로 지정
- 현재 GitHub token 사용자를 PR assignee로 지정
- 연결된 이슈의 labels를 PR에 그대로 지정
- 연결된 이슈의 Projects V2에 PR도 추가
- GitHub API 요청을 UTF-8 JSON으로 전송해 한글 깨짐 방지

## 네트워크 승인 최소화

- 기본 확인은 `inspect --local`로 수행해 GitHub API를 호출하지 않는다.
- GitHub API 호출은 PR 생성/갱신 직전 `finish` 명령 한 번으로 모은다.
- 네트워크 승인이 필요하면 `python .agents\skills\issue-pr\scripts\issue_pr.py finish ...` 형태로 한 번만 요청한다.
- 승인 요청 시 가능한 경우 `prefix_rule`은 `["python", ".agents\\skills\\issue-pr\\scripts\\issue_pr.py"]`로 제안해 이후 같은 스크립트의 GitHub API 호출을 반복 승인 없이 처리한다.
- `git push`는 별도 Git 명령이므로 이미 승인된 `["git", "push"]` prefix를 사용한다.
- `inspect` 또는 `upsert`를 여러 번 나눠 호출해 네트워크 승인을 반복하지 않는다.

## GitHub 토큰 준비

- `gh` CLI가 없거나 인증되어 있지 않은 환경에서는 `GITHUB_TOKEN`이 필요하다.
- 토큰은 GitHub Personal Access Token을 사용하고, 이 저장소의 이슈/PR을 읽고 쓸 수 있는 권한이 있어야 한다.
- 연결된 이슈의 Projects V2까지 PR에 복사하려면 token에 Projects 접근 권한도 필요하다.
- 우선순위는 환경 변수 `GITHUB_TOKEN`이 가장 높고, 없으면 저장소 루트의 `.env.local`에서 읽는다.
- `.env.local`에는 아래처럼 한 줄로 저장한다.
  - `GITHUB_TOKEN=ghp_xxx`
- `.env.local`은 커밋하지 않는다. 이 저장소에서는 `.gitignore`에 포함되어 있어야 한다.
- 토큰이 없으면 스크립트는 작업을 중단하고, 환경 변수 또는 `.env.local` 설정을 안내해야 한다.
- 토큰 값은 어떤 출력에도 노출하지 않는다.

## 작업 절차

0. 실행 조건을 확인한다.
   - 이 스킬은 사용자가 `$issue-pr` 또는 같은 의미의 명시적 PR 요청을 했을 때만 실행한다.
   - 단순 수정 요청 중에는 이 스킬 절차를 시작하지 않는다.
   - 스킬이나 코드 수정 직후에도 자동 커밋/PR 갱신을 하지 않고, 사용자가 `$issue-pr`를 다시 호출할 때까지 기다린다.

1. 브랜치와 이슈를 확인한다.
   - `python .agents/skills/issue-pr/scripts/issue_pr.py inspect --local --base dev`
   - 스크립트 출력의 `branch`, `issue`, `commit_type`, `commits`, `changed_files`를 확인한다.
   - 브랜치명에 이슈 번호가 없거나 토큰을 찾지 못하면 중단하고 필요한 조치를 보고한다.

2. 작업트리를 검토한다.
   - `git status --short`와 `git diff`를 확인한다.
   - 이미 staged 된 변경도 사용자 작업일 수 있으므로 무조건 포함하지 않는다.
   - unstaged, staged, untracked 변경을 모두 확인하고 남은 code diff를 최대한 커밋 대상으로 정리한다.
   - 이슈와 무관하거나 저장소에 들어가면 안 되는 파일만 제외한다.
   - 사용자가 남은 diff 전체 처리를 승인했으면 생성물과 캐시도 제외하지 말고 포함 여부를 명시적으로 판단한다.

3. 검증을 실행한다.
   - 변경 범위에 맞는 focused test 또는 문서 검증을 실행한다.
   - 문서/스킬 변경이면 최소한 `git diff --check -- <관련 파일>`을 실행한다.
   - 실행하지 못한 검증은 PR 본문 `참고 사항`에 이유와 함께 남긴다.

4. 커밋을 만든다.
   - 브랜치 prefix가 `feat`, `fix`, `refactor` 중 하나면 커밋 타입은 이를 우선한다.
   - 그 외에는 실제 변경에 따라 `docs`, `style`, `test`, `chore`, `design`, `comment`, `rename`, `remove` 중 고른다.
   - 메시지는 `type : 한글 요약` 형식으로 작성한다.
   - 남은 diff가 모두 승인된 변경이면 `git add -A`로 전체 반영한다.
   - 일부만 포함해야 하면 관련 파일만 stage 하거나 `git commit --only -- <파일...>`로 커밋 범위를 제한한다.

5. PR 본문을 준비한다.
   - `origin/dev..HEAD` 전체 커밋과 diff 통계를 다시 확인한다.
   - 아래 명령으로 템플릿 기반 PR 본문 초안을 만든다.
     - `python .agents/skills/issue-pr/scripts/issue_pr.py render --base dev --checks "<실행한 검증>" --excluded "<제외한 변경>"`
   - 초안의 `작업 사항`이 전체 브랜치 변경을 반영하는지 확인하고, 필요하면 더 구체적인 한글 요약을 `--summary`로 넘긴다.

6. PR을 생성하거나 갱신한다.
   - 브랜치를 원격에 push한다.
   - 아래 명령 한 번으로 열린 PR을 갱신하거나 새 PR을 만든다.
     - `python .agents/skills/issue-pr/scripts/issue_pr.py finish --base dev --checks "<실행한 검증>" --excluded "<제외한 변경>"`
   - 추가 작업을 PR 제목에 명시해야 하면 `--title-extra "<추가작업>"`를 사용한다.
   - `--title`은 수동 override가 반드시 필요할 때만 사용한다.
   - 이미 열린 PR이 있으면 새 PR을 만들지 않고 제목과 본문을 갱신한다.
   - PR 생성/갱신 후 현재 GitHub token 사용자를 assignee로 지정한다.
   - 연결된 이슈에 이미 지정된 labels를 PR에 그대로 붙인다.
   - 연결된 이슈가 포함된 Projects V2에 PR도 추가한다.
   - Projects V2 권한이 부족하면 PR 갱신은 실패로 보지 않고, projects 복사 실패 사유를 결과에 보고한다.

## 이슈 템플릿 해석 기준

- Bug 이슈: 영향받는 동작, 환경, API, 에러 코드와 로그, 해결 방식을 확인한다.
- Feature 이슈: 계획 기능, 구현 내용, 참고 자료, DB 변경, 체크리스트를 확인한다.
- Refactor 이슈: 리팩토링 대상, 사유, 체크리스트를 확인한다.

## 결과 보고

작업이 끝나면 아래 내용을 보고한다.

- 브랜치명과 이슈 번호
- 커밋 해시와 커밋 메시지
- PR URL
- 실행한 테스트 또는 체크
- 커밋에서 제외한 변경
