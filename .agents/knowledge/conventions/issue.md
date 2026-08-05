# 이슈 템플릿·관례

## Scope

`.github/ISSUE_TEMPLATE/`의 기존 GitHub Issue 템플릿을 coding agent가 그대로 선택·작성하도록 안내한다. 제목은 실제 이슈 이력에서 확인된 아이콘 관례를 따른다.

## Read When

- GitHub Issue를 생성하거나 제목·본문을 수정할 때
- Issue Type에 맞는 기존 템플릿을 고를 때
- `.github/ISSUE_TEMPLATE/**` 또는 `create-issue` 스킬이 바뀐 때

## Authoritative Sources

- `.github/ISSUE_TEMPLATE/bug-template.md`
- `.github/ISSUE_TEMPLATE/feautre-template.md`
- `.github/ISSUE_TEMPLATE/refactor-template.md`
- `.agents/skills/create-issue/references/issue-templates.md`
- 실제 GitHub Issue 제목 이력

## Current Implementation

아래는 `.github/ISSUE_TEMPLATE/`에 있는 현재 템플릿의 구조다. 실제 작성 시에는 이 문서의 복사본보다 위 Authoritative Sources의 현재 파일을 우선한다.

### Bug

Source: `.github/ISSUE_TEMPLATE/bug-template.md`

````markdown
---
name: Bug Template
about: 발견한 버그 사항
title: ''
labels: ''
assignees: ''

---

## 🛠️ 발견된 버그 기능
<!--어떤 부분에서 버그가 나오는지 기입합니다.-->

## 🌎 발견된 환경
- 서버 (dev, prod):
- 발생 API:
- 에러 코드:

## 💻 에러 로그
<!--에러 로그를 기입합니다.-->
```
```
## 💡 해결방안
<!--해당 에러를 어떻게 해결할 것인지, 어떻게 임시적 처리를 진행해야 하는지 상세히 기입합니다.-->
````

### Feature

Source: `.github/ISSUE_TEMPLATE/feautre-template.md`

```markdown
---
name: Feautre Template
about: 새롭게 개발할 기능
title: ''
labels: ''
assignees: ''

---

## 🛠️ 계획된 개발 기능
<!--어떠한 기능 / 화면을 만드는지 적습니다.-->

## 🛠 기능 구현 세부사항
<!--해당 기능들이 요구하는 사항 등을 적습니다.-->

## 🛠 참고사항
<!--해당 기능들에 있어 특이사항을 적습니다.-->

## 💾 DB 변경사항
<!--DB 변경사항을 적습니다.-->

## 📝 check-lists
- [ ]
```

### Refactor

Source: `.github/ISSUE_TEMPLATE/refactor-template.md`

```markdown
---
name: Refactor Template
about: 기존 기능을 개선
title: ''
labels: ''
assignees: ''

---

## 🛠️ 계획된 리팩토링할 기능
<!--어떠한 기능 / 화면을 리팩토링하는지 적습니다.-->

## 🛠 사유
<!--해당 기능에서 "왜?" 리팩토링하는지 적습니다.-->

## 📝 check-lists
- [ ]
```

실제 이슈 이력에서 확인된 제목 prefix는 다음과 같다.

- Bug: `🐛 Bug - `
- Feature: `✨ Feature - `
- Refactor: `🎨 Refactor - `
- Task: `🔧 Task - `

## Invariants

- 이슈 본문은 Type에 맞는 `.github/ISSUE_TEMPLATE/` 원본을 기준으로 작성한다.
- 제목은 실제 이력에서 확인된 아이콘과 Type prefix를 생략하지 않는다.
- 템플릿의 주석을 실제 내용으로 바꾸되, 알 수 없는 내용은 추측해 채우지 않는다.
- 기존 템플릿에 없는 항목·Type·아이콘을 임의로 추가하지 않는다.

## Known Gaps

- Task와 Epic은 `.github/ISSUE_TEMPLATE/` 원본 템플릿이 없다.
- 현재 저장소 Issue 이력에서 Epic 아이콘은 확인하지 못했다.
- Feature 템플릿 파일명은 실제로 `feautre-template.md`로 저장돼 있다.

## Update When

`.github/ISSUE_TEMPLATE/**`의 frontmatter, 본문 항목·주석, 실제 이슈 제목 prefix 관례가 바뀐 때 갱신한다.

## Validation

- `Get-Content .github/ISSUE_TEMPLATE/*.md -Encoding utf8`
- `Get-Content .agents/skills/create-issue/references/issue-templates.md -Encoding utf8`
- GitHub API로 최근 Issue 제목 prefix 확인

