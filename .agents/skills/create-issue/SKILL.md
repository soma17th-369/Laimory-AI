---
name: create-issue
description:
  사용자가 할 일·작업·기능·버그를 말하면 그것을 잘 정리된 GitHub 이슈로 만들어 준다.
  제목/세부 내용을 작성하고, Issue Type(Bug/Epic/Feature/Task)을 정하고, Priority와 Size
  필드를 설정한다. 작업 규모가 크면 Epic으로 만들고 하위 Feature/Task로 쪼개 sub-issue로 연결한다.
  "이슈 만들어줘", "작업 등록", "할 일 추가", "이거 이슈로", "GitHub 이슈 생성", "백로그에 넣어줘",
  "버그 등록", "티켓 만들어", "create an issue", "file a ticket", "add this task" 같은 요청은 물론,
  사용자가 명시적으로 '이슈'라는 단어를 쓰지 않더라도 "이거 나중에 해야 하는데", "이 기능 추가하자",
  "이 버그 잡아야 함" 처럼 추적해야 할 작업을 꺼내면 적극적으로 이 스킬을 사용한다.
  단, 단순히 코드를 지금 바로 고치는 작업이면 트리거하지 않는다.
---

# create-issue

사용자가 꺼낸 "할 일"을 받아서 **잘 정리된 GitHub 이슈**로 만든다. 단순 생성이 아니라
(1) 세부 내용을 충실히 작성하고, (2) 알맞은 **Type**을 부여하고, (3) **Priority/Size** 필드를
채우고, (4) 규모가 크면 **Epic으로 쪼개 sub-issue**까지 연결하는 것이 목표다.

이 스킬의 대상 조직은 `soma17th-369`이고, 그 안에 레포가 여러 개다. 이슈를 만들 **레포는 매번
정한다**(5단계 참고). 다른 조직에서 쓰려면 환경변수 `CREATE_ISSUE_ORG`로 바꾼다.

## 핵심 원칙: 모호하면 반드시 사람에게 묻는다

이슈는 한번 만들면 팀원들이 보고 일의 우선순위를 판단하는 근거가 된다. 그래서 **추측으로
잘못된 Type·Priority·Size를 박는 것보다, 잠깐 멈추고 사람에게 확인받는 편이 훨씬 낫다.**

판단 요소(Type / 분해 여부 / Priority / Size) 중 **조금이라도 애매한 게 있으면 `AskUserQuestion`
으로 선택지를 띄워 사람이 고르게 한다.** 절대 애매한 채로 진행하지 않는다. 확실한 것만 자동으로
정하고, 나머지는 묻는다. 마지막에 **무엇을 만들지 요약해 확인받은 뒤** 실제 생성한다(이슈는
실제로 만들어지는 부작용이 있으므로).

## 사전 점검 (본문 작성·질문 전에 먼저)

인증부터 확인한다. 안 그러면 본문 다 쓰고 질문 다 받은 뒤 *마지막 생성 단계*에서 막혀 헛수고가 된다.

1. **로그인**: `gh auth status`를 실행한다. 로그인이 안 돼 있으면 사용자에게 `gh auth login`을
   안내하고 멈춘다(추측으로 진행하지 말 것).
2. **project 스코프**: 이슈를 **프로젝트에 추가하거나 Size를 설정할 때만** `project` 스코프가
   필요하다(이슈 생성·Type·Priority엔 불필요). `gh auth status`의 `Token scopes`에 `project`가
   없고 이번에 프로젝트/Size를 쓸 거라면, `gh auth refresh -s project`를 안내하고 사용자가 마친 뒤 진행한다.
3. 그 외 gh 에러가 나면 스크립트가 **gh의 원본 메시지를 그대로 출력**한다. 그 메시지와 거기 담긴
   해결법(예: `gh auth refresh ...`)을 **사용자에게 그대로 전달**하고 지시를 기다린다. 임의로 우회하지 말 것.

> 왜 미리 보나: 이슈 생성은 `project` 스코프 없이도 되지만, 그 *다음* 프로젝트 추가 단계에서
> 스코프가 없으면 **이슈만 만들어지고 프로젝트엔 안 들어간 어중간한 상태**가 될 수 있다.
> 프로젝트/Size를 쓸 거면 스코프를 먼저 확인해 이 상황을 피한다.

헬퍼 스크립트(`scripts/gh_issue.py`)는 타입/옵션 ID를 org admin이 바꿔도 깨지지 않게 **이름으로
매번 동적 해석**하므로, 너는 이름(Feature, High, M …)만 넘기면 된다.

## 작업 흐름

### 1단계 — 작업 이해 & 세부 내용 작성

사용자 입력이 한 줄이어도, 이슈 본문은 읽는 사람이 바로 착수할 수 있을 만큼 구체적으로 쓴다.
한국어로 작성한다.

**Type별 정해진 본문 템플릿을 우선 쓴다.** Type을 정한 뒤(2단계) 대응 템플릿이 있으면 그 골격을
채워 본문을 만든다. 템플릿 전문은 [references/issue-templates.md](references/issue-templates.md) 참조.

| Type | 본문 템플릿 |
|---|---|
| **Bug** | Bug 템플릿 |
| **Feature** | Feature 템플릿 |
| **Task** (리팩터링 성격) | Refactor 템플릿 |
| **Task** (그 외) · **Epic** | 아래 일반 골격 |

대응 템플릿이 없으면(그 외 Task, Epic) 아래 일반 골격을 쓴다(내용이 없으면 섹션 생략 가능):

```markdown
## 배경 / 목적
왜 필요한지, 어떤 문제를 푸는지.

## 상세 내용
무엇을 어떻게 할지. 필요하면 체크리스트로.
- [ ] ...

## 완료 조건 (Acceptance Criteria)
이게 충족되면 끝났다고 볼 수 있는 기준.
```

템플릿을 쓸 때는 주석(`<!-- ... -->`)을 실제 내용으로 바꾸고, 알 수 없는 항목은 빈칸으로 남겨
작성자가 채우게 둔다(빈 섹션을 통째로 지우지 말 것). 레포 컨벤션이 본문에 영향을 준다면
`CLAUDE.md` 등을 참고한다. 본문은 임시 `.md` 파일로 저장해 `--body-file`로 넘긴다.

### 2단계 — Type 결정 (Bug / Epic / Feature / Task)

| Type | 의미 |
|---|---|
| **Bug** | 예상치 못한 오류 또는 비정상 동작 이슈 |
| **Epic** | 여러 Feature와 Task를 묶는 상위 이슈 |
| **Feature** | 사용자가 직접 경험하는 새로운 기능 이슈 |
| **Task** | 기능 외 기술적·운영적 작업 이슈 |

입력만으로 Type이 명확하면 그대로 정한다. **둘 사이에서 애매하면(예: "리팩터링 + 신기능"이
섞여 Feature인지 Task인지 모호) 묻는다.**

### 3단계 — Epic 여부 & 분해 판단

작업 **볼륨이 크면**(여러 독립 기능/단계가 묶여 있고, 한 이슈로는 추적이 버거운 규모) Epic으로
만든 뒤, 너가 **Feature/Task 단위로 쪼개** 각각을 이슈로 만들고 Epic의 **sub-issue로 연결**한다.

- 규모가 큰지 **애매하면 묻는다**: "이 작업을 하나의 이슈로 둘까요, Epic으로 쪼갤까요?" 그리고
  Epic으로 간다면 **네가 제안하는 분해안(하위 Feature/Task 목록)을 먼저 보여주고 확인받는다.**
  사람이 일을 어떻게 나눌지에 대한 감이 있으므로, 분해는 항상 사람 확인을 거친다.
- 각 하위 이슈도 자기 Type/Priority/Size를 갖는다(같은 흐름을 재귀적으로 적용).
- 생성 순서: **Epic 먼저 생성 → 각 하위 이슈를 `--parent <epic번호>`로 생성**(자동 연결).

### 4단계 — Priority 결정

| Priority | 의미 |
|---|---|
| **Critical** | 즉시 처리가 필요한 긴급 이슈 |
| **High** | 이번 주기 내 반드시 완료해야 하는 이슈 |
| **Medium** | 일반적인 우선순위의 이슈 |
| **Low** | 여유가 있을 때 처리해도 되는 이슈 |
| **Backlog** | 당장 처리하지 않아도 되는 이슈 |

사용자 입력에 긴급도 신호가 분명하면 정하고, **조금이라도 모호하면 `AskUserQuestion`으로 묻는다.**
옵션은 5개지만 `AskUserQuestion`은 질문당 4개까지만 받으므로, 제시 방법은 아래 "묻는 방식" 참고.

### 5단계 — 레포 & 프로젝트 결정

이슈가 *어디에* 생기는지 두 가지를 정한다. 둘 다 추측하지 말 것.

**레포 (필수)** — `gh issue create`는 대상 레포를 요구하고, org에 레포가 여러 개라 하나로 고정할 수 없다.
- 사용자가 명시했으면(예: "server 레포에", "Laimory-android에") 그대로 쓴다.
- **명시 안 했으면 묻는다.** `python .claude/skills/create-issue/scripts/gh_issue.py repos`
  로 org 레포 목록을 받아 `AskUserQuestion`으로 고르게 한다. (org는 고정이라 레포 *이름*만 넘기면 됨.)
- 결정된 레포는 `--repo <name>`으로 넘긴다.

**프로젝트 (선택)** — 프로젝트마다 일을 보는 맥락이 달라, 잘못된 보드에 넣으면 추적이 어긋난다.
- 사용자가 명시했으면(예: "project 4에", "시스템 초기 설계 및 구축에") 그대로 쓴다.
- **명시 안 했으면 묻는다.** `... gh_issue.py projects`로 목록(number/title)을 받아
  `AskUserQuestion`으로 고르게 한다. "프로젝트 없이 레포 이슈로만 둘까요?" 선택지도 함께 제공한다.
  항목이 4개를 넘으면 4-옵션 제약상 가능성 높은 것만 띄우고 나머지는 Other로 받는다.
- `--project <번호 또는 제목>`으로 넘긴다. 안 넣기로 했으면 생략한다(이 경우 Size도 설정 불가).

> 참고: Type·Priority·프로젝트는 전부 **org 레벨**이라 어느 레포를 골라도 동일하게 동작한다.
> 레포는 단지 "이슈가 어느 레포에 사느냐"만 결정한다.

### 6단계 — Size 결정

Size 옵션은 **XS / S / M / L / XL**. 작업량 감이 명확하면 정하고, **애매하면 선택지를 띄워 묻는다.**
Size는 **프로젝트 필드**라 5단계에서 고른 프로젝트가 있어야 설정된다(`--size`는 반드시 `--project`와
함께 넘긴다). 프로젝트에 안 넣기로 했거나 Size가 필요 없으면 Size는 생략한다.

### 7단계 — 계획 확인 후 생성

만들기 직전에 요약을 보여주고 확인받는다. 예:

```
다음으로 만들겠습니다:
- 제목: 로그인 구현
- Type: Feature / Priority: High / Size: M
- 프로젝트: 시스템 초기 설계 및 구축
- 본문: (배경·상세·완료조건 요약)
진행할까요?
```

Epic이면 Epic + 하위 목록 전체를 보여준다. 확인되면 스크립트로 생성한다.

## 스크립트 사용법

```bash
# 발견용: 어디에 만들지 사용자에게 물을 때 목록 받기
python .claude/skills/create-issue/scripts/gh_issue.py repos       # org 레포 이름 목록
python .claude/skills/create-issue/scripts/gh_issue.py projects    # org 프로젝트 목록

# 단일 이슈 (--repo 필수, --project/--size 선택)
python .claude/skills/create-issue/scripts/gh_issue.py create \
  --repo Laimory-server --title "로그인 구현" --body-file /tmp/body.md \
  --type Feature --priority High --size M --project "시스템 초기 설계 및 구축"

# Epic의 하위 이슈로 붙이며 생성 (Epic을 먼저 만들고 그 번호를 --parent로)
python .claude/skills/create-issue/scripts/gh_issue.py create \
  --repo Laimory-server --title "JWT 발급/검증" --body-file /tmp/sub1.md \
  --type Task --priority High --parent 12

# 이미 있는 두 이슈 연결
python .claude/skills/create-issue/scripts/gh_issue.py link --repo Laimory-server --parent 12 --child 13
```

- `--repo`는 `owner/name` 또는 org 내 `name`만 줘도 된다(org 자동 결합). 생략하면 안내 후 중단된다.
- `--size`/`--project`를 생략하면 프로젝트에 추가하지 않고 Type/Priority만 설정한다.
- 출력은 JSON 한 줄(`{"number":..,"url":..}`)이라, Epic 생성 결과의 `number`를 하위 이슈의
  `--parent`로 넘기면 된다.
- 이름 해석에 실패하면(`[해석 실패] …`) 스크립트가 사용 가능한 값 목록을 알려주니, 그 목록으로
  사용자에게 다시 묻고 재시도한다.

## 묻는 방식 (AskUserQuestion)

Priority/Size/Type/분해여부가 모호할 때는 한 번의 `AskUserQuestion` 호출에 필요한 질문들을 모아
띄운다(질문 여러 개를 한 번에 가능). 각 선택지에는 **의미 설명을 함께** 달아 빠르게 고르게 한다.

**중요 제약: `AskUserQuestion`의 옵션은 질문당 최대 4개다.** 그런데 Priority(5개)·Size(5개)는
선택지가 5개라 전부 못 넣는다. 그러니 **맥락상 가능성이 가장 높은 4개만 제시하고, 나머지 1개는
자동 제공되는 "Other"로 받는다.** 어떤 4개를 고를지는 작업 성격으로 판단한다. 예:

- 인프라/운영 Task처럼 계획된 작업 → Priority에서 **Critical(긴급장애용)을 제외**한 High/Medium/Low/Backlog.
- 명백히 작은 작업 → Size에서 **XL을 제외**한 XS/S/M/L.

질문 문구에 "(○○은 제외 — 필요시 Other)"처럼 빠진 선택지를 안내해, 사용자가 그 값을 원하면
Other로 직접 넣을 수 있게 한다.

## 예시

**Example:**
입력: "redis ec2 구축 이슈 project 4에 만들어줘"

- 판단: Type=**Task**(인프라/운영이라 명확) · 규모 작아 단일 이슈 · 프로젝트는 사용자가 지정(project 4) ·
  **레포는 미지정이라 `repos`로 물어 Laimory-server 선택** · Priority·Size도 신호 없어 질문 → Medium/S
- 실행:
  ```bash
  python .claude/skills/create-issue/scripts/gh_issue.py create \
    --repo Laimory-server --title "Redis EC2 구축" --body-file /tmp/body.md \
    --type Task --priority Medium --size S --project "시스템 초기 설계 및 구축"
  ```
- 결과: 배경·체크리스트·완료조건을 갖춘 Task 이슈가 Priority=Medium / Size=S로 생성되어 project 4에 추가됨.

## 주의사항 (Gotchas)

이 함정들은 실제로 겪고 정리한 것이다. 메커니즘을 알면 비슷한 상황에도 대처할 수 있다.

- **"Priority"라는 이름의 필드가 둘이다.** org에는 (a) 실제 옵션(Critical~Backlog)을 가진
  **org Issue Field "Priority"** 와, (b) 프로젝트 보드에 딸린 **옵션 없는 프로젝트 필드 "Priority"**
  가 따로 있다. 값 설정은 반드시 (a) 경로(스크립트의 `setIssueFieldValue`)로 가야 한다.
  `gh project item-edit`로 (b)를 건드리면 옵션이 없어 조용히 실패한다 — 이 스킬이 직접 gh를 치지
  않고 스크립트를 거치는 이유다.
- **옵션 자체는 org admin만 편집**한다. 단, 기존 옵션을 이슈에 *값으로 지정*하는 건 일반 멤버도 된다
  (`viewerCanSetFields`). 없는 옵션 이름을 넘기면 스크립트가 `[해석 실패]` + 사용 가능 목록을 출력하니,
  그 목록 그대로 사용자에게 다시 물어 재시도한다.
- **Size는 프로젝트 필드라 프로젝트마다 없을 수 있다.** 대상 프로젝트에 Size 필드가 없으면
  `[해석 실패]`가 난다. 그땐 Size를 생략하거나 사용자에게 다른 프로젝트를 확인한다.
- **이슈는 실제로 만들어진다(되돌리기 번거롭다).** 7단계 확인을 건너뛰지 마라. 잘못 만들었으면
  `deleteIssue` mutation으로 지운다.
- **Epic 분해는 생성 순서가 중요하다.** Epic을 *먼저* 만들어 번호를 얻은 뒤, 하위 이슈를
  `--parent <번호>`로 만든다. 순서가 바뀌면 넘길 부모 번호가 없다.
- **인코딩**: 스크립트가 stdout과 gh 입출력을 UTF-8로 고정하므로, Windows 한국어 로캘에서도
  `PYTHONUTF8` 환경변수 없이 한글 제목이 안 깨진다.
