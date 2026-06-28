---
name: issue-pr
description: 현재 브랜치명의 #번호를 GitHub 이슈 번호로 사용해 변경 사항을 검토하고, 규칙에 맞는 커밋과 Pull Request를 생성하거나 갱신합니다. 사용자가 현재 작업을 커밋하고 PR로 올리라고 요청할 때 사용합니다.
---

# issue-pr

현재 브랜치 작업을 이슈와 연결된 커밋 및 Pull Request로 정리한다.

## 핵심 규칙

- 현재 브랜치명에서 `#<번호>`를 추출해 GitHub 이슈 번호로 사용한다.
- 기본 base 브랜치는 `dev`이며, 비교 범위는 `origin/dev..HEAD`다.
- 커밋 메시지와 PR 제목은 `type : 한글 요약` 형식으로 작성한다.
- PR 본문은 마지막 커밋 하나가 아니라 base 브랜치부터 현재 HEAD까지의 전체 누적 변경을 기준으로 작성한다.
- 사용자에게 보이는 커밋 메시지, PR 제목, PR 본문, 결과 보고는 한글로 작성한다.
- 관련 없는 staged 변경, 생성물, 캐시, 사용자가 만든 변경은 커밋에 섞지 않는다.
- 사용자가 명시하지 않은 파일 삭제, `git reset`, `git checkout` 같은 파괴적 작업은 하지 않는다.
- 토큰은 절대 출력, 커밋, PR 본문, 오류 보고에 포함하지 않는다.

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
- GitHub API 요청을 UTF-8 JSON으로 전송해 한글 깨짐 방지

## GitHub 토큰 준비

- `gh` CLI가 없거나 인증되어 있지 않은 환경에서는 `GITHUB_TOKEN`이 필요하다.
- 토큰은 GitHub Personal Access Token을 사용하고, 이 저장소의 이슈/PR을 읽고 쓸 수 있는 권한이 있어야 한다.
- 우선순위는 환경 변수 `GITHUB_TOKEN`이 가장 높고, 없으면 저장소 루트의 `.env.local`에서 읽는다.
- `.env.local`에는 아래처럼 한 줄로 저장한다.
  - `GITHUB_TOKEN=ghp_xxx`
- `.env.local`은 커밋하지 않는다. 이 저장소에서는 `.gitignore`에 포함되어 있어야 한다.
- 토큰이 없으면 스크립트는 작업을 중단하고, 환경 변수 또는 `.env.local` 설정을 안내해야 한다.
- 토큰 값은 어떤 출력에도 노출하지 않는다.

## 작업 절차

1. 브랜치와 이슈를 확인한다.
   - `python .agents/skills/issue-pr/scripts/issue_pr.py inspect --base dev`
   - 스크립트 출력의 `branch`, `issue`, `commit_type`, `issue_title`, `commits`, `changed_files`를 확인한다.
   - 브랜치명에 이슈 번호가 없거나 토큰을 찾지 못하면 중단하고 필요한 조치를 보고한다.

2. 작업트리를 검토한다.
   - `git status --short`와 `git diff`를 확인한다.
   - 이미 staged 된 변경도 사용자 작업일 수 있으므로 무조건 포함하지 않는다.
   - 관련 파일만 커밋 대상으로 고른다.
   - 생성물과 캐시는 제외한다.

3. 검증을 실행한다.
   - 변경 범위에 맞는 focused test 또는 문서 검증을 실행한다.
   - 문서/스킬 변경이면 최소한 `git diff --check -- <관련 파일>`을 실행한다.
   - 실행하지 못한 검증은 PR 본문 `참고 사항`에 이유와 함께 남긴다.

4. 커밋을 만든다.
   - 브랜치 prefix가 `feat`, `fix`, `refactor` 중 하나면 커밋 타입은 이를 우선한다.
   - 그 외에는 실제 변경에 따라 `docs`, `style`, `test`, `chore`, `design`, `comment`, `rename`, `remove` 중 고른다.
   - 메시지는 `type : 한글 요약` 형식으로 작성한다.
   - 관련 파일만 stage 하거나 `git commit --only -- <파일...>`로 커밋 범위를 제한한다.

5. PR 본문을 준비한다.
   - `origin/dev..HEAD` 전체 커밋과 diff 통계를 다시 확인한다.
   - 아래 명령으로 템플릿 기반 PR 본문 초안을 만든다.
     - `python .agents/skills/issue-pr/scripts/issue_pr.py render --base dev --checks "<실행한 검증>" --excluded "<제외한 변경>"`
   - 초안의 `작업 사항`이 전체 브랜치 변경을 반영하는지 확인하고, 필요하면 더 구체적인 한글 요약을 `--summary`로 넘긴다.

6. PR을 생성하거나 갱신한다.
   - 브랜치를 원격에 push한다.
   - 아래 명령으로 열린 PR을 갱신하거나 새 PR을 만든다.
     - `python .agents/skills/issue-pr/scripts/issue_pr.py upsert --base dev --title "type : 한글 요약" --checks "<실행한 검증>" --excluded "<제외한 변경>"`
   - 이미 열린 PR이 있으면 새 PR을 만들지 않고 제목과 본문을 갱신한다.

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
