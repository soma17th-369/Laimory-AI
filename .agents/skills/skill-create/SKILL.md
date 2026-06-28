---
name: skill-create
description: .agents/skills/<이름> 디렉토리를 프로젝트 스킬로 등록하고 링크 스크립트를 실행해 .claude/skills 정션까지 자동 동기화합니다. 사용자가 새 스킬을 추가하거나 "$skill-create <이름>" / "/skill-create <이름>" 형태로 스킬 생성·동기화를 요청할 때 사용합니다.
---

# skill-create

새 프로젝트 스킬을 `.agents/skills/` 아래에 만들고 Claude(`.claude/skills/`)까지 한 번에 동기화하는 메타 스킬입니다.

## 절대 규칙

- 스킬 원본(SKILL.md)은 **언제나 `.agents/skills/<이름>/SKILL.md` 에만** 만든다.
- `.claude/skills/` 아래에는 **어떤 파일도 직접 만들지 않는다.** 그쪽은 정션(junction)일 뿐이며, 원본을 `.claude`에 두면 단일 원본 원칙이 깨진다.
- `.claude` 동기화는 **반드시 `scripts/link-skills.ps1`(Windows) 또는 `scripts/link-skills.sh`(mac/linux)** 로만 한다. 손으로 정션/링크를 만들지 않는다.

## 인자

`<이름>` : 스킬이자 디렉토리 이름. kebab-case 권장 (예: `pr-summary`).
호출 예: `/skill-create pr-summary`

## 절차

1. `<이름>` 인자를 확인한다. 없으면 사용자에게 스킬 이름을 묻는다.
2. `.agents/skills/<이름>/` 존재를 확인하고, 없으면 디렉토리를 만든다.
3. `.agents/skills/<이름>/SKILL.md` 를 확인한다.
   - 이미 있으면: frontmatter의 `name` 이 `<이름>` 과 일치하고 `description` 이 비어 있지 않은지 검증한다. 어긋나면 사용자에게 알리고 고친다.
   - 없으면: 아래 템플릿으로 새로 만든다. `description` 은 사용자가 준 설명으로 채우고, 없으면 한 줄 물어본다.
4. 링크 스크립트를 실행한다.
   - Windows: `powershell -ExecutionPolicy Bypass -File scripts/link-skills.ps1`
   - mac/linux: `bash scripts/link-skills.sh`
5. `.claude/skills/<이름>` 가 `Junction`(ReparsePoint)으로 생성됐고, `.agents/skills/<이름>` 를 가리키는지 확인한다. (`Get-Item .claude/skills/<이름> | Select LinkType,Target`)
6. 결과를 보고한다: 원본 경로, 정션 동기화 여부, 다음에 Claude가 이 스킬을 인식하려면 세션을 새로 고치면 된다는 안내.

## 주의

- 기존 스킬의 SKILL.md 를 덮어쓰지 않는다. 이미 내용이 있으면 사용자 확인 없이 수정하지 않는다.
- `.claude` 쪽에서 무언가 깨져 보여도(빈 파일·일반 파일) 손으로 고치지 말고 링크 스크립트를 재실행한다.

## SKILL.md 템플릿

```markdown
---
name: <이름>
description: <이 스킬이 무엇을 하고 언제 호출되는지 한 줄로. 트리거 상황을 포함한다.>
---

# <이름>

<스킬 설명>

## 절차

1. ...
2. ...
```
