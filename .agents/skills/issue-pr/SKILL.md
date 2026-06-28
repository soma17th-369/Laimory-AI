---
name: issue-pr
description: 현재 브랜치명의 #번호를 GitHub 이슈 번호로 사용해 변경 사항을 검토하고, 규칙에 맞는 커밋과 Pull Request를 생성하거나 갱신합니다.
---

# issue-pr

현재 브랜치의 작업을 이슈와 연결된 커밋 및 Pull Request로 정리하는 스킬입니다.

## 기본 규칙

- 현재 브랜치명에서 `#<번호>` 패턴을 찾아 GitHub 이슈 번호로 사용한다.
- 작업 브랜치는 `dev`에서 분기된 `feat/#33`, `fix/#25`, `refactor/#10` 같은 형식을 기준으로 한다.
- 모든 사용자에게 보이는 커밋 메시지, PR 제목, PR 본문은 한글로 작성한다.
- 커밋 메시지 형식은 반드시 `type : 한글 요약`을 사용한다. 예: `feat : 로그인 API 추가`
- 변경 전에는 관련 파일과 diff를 읽고, 관련 없는 사용자 변경은 커밋하지 않는다.
- 사용자가 명시하지 않은 삭제, `git reset`, `git checkout` 같은 파괴적 명령은 사용하지 않는다.
- 브랜치명에 `#<번호>`가 없으면 작업을 중단하고 이슈 번호나 올바른 브랜치를 요청한다.
- 기본 PR 대상 브랜치는 `dev`다. 사용자가 다른 base를 명시한 경우에만 바꾼다.

## GitHub 접근 규칙

- `gh` CLI가 설치되어 있고 인증되어 있으면 우선 사용한다.
- `gh`가 없거나 사용할 수 없으면 GitHub API와 `GITHUB_TOKEN`을 사용한다.
- `GITHUB_TOKEN`은 환경 변수에서 먼저 찾고, 없으면 저장소 루트의 `.env.local`에서 `GITHUB_TOKEN=...` 형식으로 읽는다.
- 토큰은 절대 출력, 커밋, PR 본문, 커밋 메시지, 오류 보고에 포함하지 않는다.
- Windows PowerShell에서 GitHub API로 한글 JSON을 보낼 때는 문자열 본문을 그대로 넘기지 말고 UTF-8 바이트로 전송한다.
  - 권장 방식: `$json = $payload | ConvertTo-Json -Depth 10; $bytes = [System.Text.Encoding]::UTF8.GetBytes($json); Invoke-RestMethod ... -Body $bytes -ContentType 'application/json; charset=utf-8'`
  - 이 규칙은 PR 제목과 본문이 `??`로 깨지는 문제를 막기 위한 필수 절차다.
- GitHub API 호출이 네트워크 제한으로 실패하면 승인 요청을 통해 동일 작업을 다시 시도한다.
- `gh`와 토큰을 모두 사용할 수 없으면 차단 사유와 필요한 설정을 정확히 보고한다.

## 작업 절차

1. 브랜치와 이슈를 확인한다.
   - `git branch --show-current`로 현재 브랜치를 확인한다.
   - 브랜치명에서 `#(\d+)`로 이슈 번호를 추출한다.
   - `gh issue view <번호> --comments` 또는 GitHub API `GET /repos/{owner}/{repo}/issues/{번호}`로 이슈 내용을 읽는다.
   - 브랜치 prefix가 `feat`, `fix`, `refactor` 중 하나면 커밋 타입은 이 값을 우선한다.
   - 브랜치 prefix와 이슈 템플릿 성격이 다르면 브랜치 prefix를 우선하고, 결과 보고에 불일치를 남긴다.

2. 현재 작업 상태를 검토한다.
   - `git status --short`로 변경 파일과 staged 상태를 확인한다.
   - `git diff`와 필요한 경우 `git diff --cached`를 확인한다.
   - 이미 staged 된 변경도 사용자 작업일 수 있으므로 무조건 포함하지 않는다.
   - 생성물, 캐시, 의존성 lockfile, 관련 없는 파일 삭제가 있으면 커밋 대상에서 제외하거나 사용자 확인을 받는다.
   - focused test 또는 문서 변경 검증을 실행한다. 실행하지 못하면 PR 본문에 이유를 적는다.

3. PR에 들어갈 전체 변경 범위를 산정한다.
   - PR 본문은 마지막 커밋 하나가 아니라 base 브랜치부터 현재 HEAD까지의 전체 누적 변경을 기준으로 작성한다.
   - 기본 비교 범위는 `origin/dev..HEAD`다. 원격 정보가 낡았을 수 있으면 가능하면 `git fetch origin dev` 후 다시 계산한다.
   - 다음 명령으로 전체 커밋과 파일 변경을 확인한다.
     - `git log --oneline origin/dev..HEAD`
     - `git diff --stat origin/dev..HEAD`
     - 필요하면 `git diff --name-status origin/dev..HEAD`
   - PR 요약에는 이 누적 범위에서 사용자에게 의미 있는 작업을 모두 반영한다.
   - 중간 커밋 메시지에 깨진 한글이 있거나 내용이 부족하면 diff와 파일 내용을 기준으로 사람이 읽을 수 있는 한글 요약으로 재작성한다.

4. 커밋을 만든다.
   - 관련 파일만 stage 한다. 이미 staged 된 관련 없는 변경은 건드리지 말고 `git commit --only -- <파일...>` 같은 방식으로 현재 커밋 대상에서 제외한다.
   - 커밋 타입은 브랜치 prefix를 우선하고, 없으면 실제 변경에 따라 아래 기준으로 고른다.
     - `feat`: 기능 추가
     - `fix`: 버그 수정
     - `docs`: 문서 변경
     - `style`: 포맷팅만 변경
     - `refactor`: 리팩토링
     - `test`: 테스트 추가 또는 수정
     - `chore`: 패키지, 설정, 기타 작업
     - `design`: UI 디자인 또는 CSS 변경
     - `comment`: 주석 추가 또는 수정
     - `rename`: 파일 또는 폴더명 변경만 수행
     - `remove`: 파일 또는 폴더 삭제만 수행
   - 커밋 메시지는 `type : 한글 요약` 형식으로 짧게 작성한다.

5. PR을 생성하거나 갱신한다.
   - 브랜치가 원격에 없거나 뒤처져 있으면 `git push -u origin <현재-브랜치>`로 push 한다.
   - 이미 열린 PR이 있는지 먼저 확인한다.
     - `gh pr view --json url,number,title,body` 또는 GitHub API `GET /repos/{owner}/{repo}/pulls?head={owner}:{branch}&base=dev&state=open`
   - 열린 PR이 있으면 새 PR을 만들지 말고 제목과 본문을 갱신한다.
   - 열린 PR이 없으면 PR을 새로 만든다.
   - PR 제목은 커밋 메시지 형식과 동일하게 `type : 한글 요약`으로 작성한다.
   - PR 본문은 반드시 저장소의 PR 템플릿 형식을 따른다.

## Pull Request 템플릿

PR 본문은 아래 형식을 유지한다. 항목이 없으면 `없음`이라고 적는다.

```markdown
## 관련 이슈
<!-- 해결한 문제를 지정하는 Issue Index에 연결해야 합니다. -->

- Resolves : #<이슈번호>

## 작업 사항
<!-- 해당 Pull Request에서 수행한 작업 목록을 제시해야 합니다. -->

- <origin/dev..HEAD 전체 변경 기준 작업 1>
- <origin/dev..HEAD 전체 변경 기준 작업 2>

## DB 변경 사항
<!-- 작업 사항이 DB에 영향이 있는 작업이라면 변경 사항을 적어야 합니다. -->

없음

## 참고 사항
<!-- 기능을 만들기 위해 다른사람들이 참고해야할 사항을 적습니다. -->

- 테스트/체크: <실행한 명령 또는 Not run과 사유>
- 제외한 변경: <커밋에서 제외한 생성물 또는 관련 없는 변경>

## 변경된 API
<!-- 프론트엔드 개발자와 공유하기 위해 텔레그램을 통해 공유될 API 변동사항을 적습니다. ex) [API description](명세서 링크) -->

없음
```

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
