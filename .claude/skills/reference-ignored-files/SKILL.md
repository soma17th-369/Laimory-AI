---
name: reference-ignored-files
description: 현재 Git worktree에 없는 gitignored 로컬 자원을 주 워크트리 원본에 연결한다. `.env`, `runtime-env.json` 같은 파일은 하드링크로, `.venv` 같은 디렉터리는 정션으로 만들어 복사 없이 현재 worktree에서 사용해야 할 때 적용한다. 추적 파일 복원이나 새 설정 생성에는 사용하지 않는다.
---

# reference-ignored-files

현재 worktree에 빠진 로컬 전용 자원을 주 워크트리의 같은 상대 경로에 연결한다. 연결된 경로는 현재 worktree 파일 탐색기에 나타나고, `.gitignore`에 포함되어 있으면 ignored 항목으로 표시된다.

## 대상 선택

- 사용자가 경로를 지정하면 그 경로만 처리한다.
- 경로를 지정하지 않으면 `.env`, `.env.local`, `.env.dev`, `runtime-env.json`, `.venv`, `.agents/plans`, `.agents/worklog` 중 현재 worktree에는 없고 주 워크트리에는 있는 항목만 후보로 삼는다.
- `.gitignore` 전체를 훑어 cache, 테스트 임시물, 출력 디렉터리까지 자동 연결하지 않는다.
- 저장소 루트 기준 상대 경로만 허용하고, 실제 ignore 규칙에 포함된 항목만 연결한다.

## 연결 실행

Windows PowerShell에서 [scripts/link_ignored_paths.ps1](scripts/link_ignored_paths.ps1)을 현재 worktree를 작업 디렉터리로 두고 실행한다.

```powershell
& "<skill-dir>\scripts\link_ignored_paths.ps1"
& "<skill-dir>\scripts\link_ignored_paths.ps1" -TargetPath @(".env", ".env.dev", ".venv")
```

첫 번째 명령은 기본 후보를 모두 처리하고, 두 번째 명령은 지정한 경로만 처리한다. 스크립트는 `git rev-parse --path-format=absolute --git-common-dir`로 주 워크트리를 찾고 모든 대상을 먼저 검증한 다음 연결한다.

- 파일: 같은 NTFS 파일을 가리키는 하드링크
- 디렉터리: 주 워크트리 디렉터리를 가리키는 정션
- 이미 현재 경로가 존재하면 종류와 관계없이 건너뛰며 삭제하거나 덮어쓰지 않는다.
- 주 워크트리 원본이 없거나, ignore 대상이 아니거나, 저장소 밖 경로이면 아무 링크도 만들기 전에 실패한다.

## 안전 규칙

- `.env`, credential, token 등 비밀값의 본문을 로그·관측·응답에 출력하지 않는다. 경로와 존재 여부만 보고한다.
- 연결 뒤 어느 경로에서 수정해도 주 워크트리 원본이 즉시 바뀐다는 점을 사용자에게 알린다.
- `.venv` 정션에서 의존성을 설치·제거하면 모든 연결 worktree가 영향을 받는다. 서로 다른 dependency 상태가 필요하면 연결하지 말고 worktree별 `.venv`를 만든다.
- `.agents/plans`와 `.agents/worklog` 정션은 계획과 작업 기록을 주 워크트리 한 곳에 모은다. 어느 worktree에서 작성해도 같은 원본에 저장된다.
- 링크 제거 요청은 링크 경로만 대상으로 해야 하며 원본을 삭제하지 않는다. 사용자가 명시적으로 요청하기 전에는 제거하지 않는다.
- Git에서 ignored 파일의 내용을 복원할 수 있다고 가정하지 않는다. 이 스킬은 주 워크트리에 이미 존재하는 로컬 원본만 참조한다.

완료 시 생성된 하드링크·정션과 건너뛴 기존 경로를 구분해서 알린다. 비밀값은 요약에 포함하지 않는다.
