# Laimory-AI 에이전트 지침

이 저장소는 FastAPI 기반 Python 서버 프로젝트입니다. Codex와 Claude가 같은 프로젝트 지침을 공유할 수 있도록 이 파일을 공통 기준으로 사용합니다.

## 기본 작업 방식
- 모든 md 파일은 한글을 base 로 생성합니다.
- 변경 전에는 관련 파일을 먼저 읽고 현재 구조를 기준으로 판단합니다.
- 불필요한 리팩터링이나 unrelated 변경은 하지 않습니다.
- 사용자가 명시하지 않은 파일 삭제, git reset, checkout 같은 파괴적 작업은 하지 않습니다.
- 기존 변경사항이 있으면 사용자 작업으로 보고 되돌리지 않습니다.

## Python 환경

- Python 버전은 `.python-version`과 `pyproject.toml` 기준을 따릅니다.
- 이 프로젝트는 `uv`와 `.venv`를 사용합니다.
- 의존성 설치는 프로젝트 루트에서 `uv sync`를 사용합니다.
- Windows에서 기본 uv 캐시 권한 문제가 있으면 다음처럼 로컬 캐시를 사용합니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv sync
```

## 실행

FastAPI 앱 진입점은 `app.server:app`입니다.

```powershell
$env:UV_CACHE_DIR=".uv-cache"
uv run uvicorn app.server:app --reload
```

## 스킬 공유

- Codex용 프로젝트 스킬 원본은 `.agents/skills/` 아래에 둡니다.
- Claude 쪽에서 공유할 때는 `.agents/skills/` 내용을 `.claude/skills/`로 복사해 동기화합니다.
- `.claude/skills/`는 링크가 아니라 복사본이며, 필요할 때 `scripts/link-skills.ps1` 또는 `scripts/link-skills.sh`를 다시 실행해 갱신합니다.
- 여기서 "공유"는 **Codex ↔ Claude 사이의 공유**입니다. `.agents/`와 `.claude/`는 개인 영역이라 저장소에 올리지 않습니다(`.gitignore`). 따라서 새로 클론한 환경에는 스킬이 없습니다.
- 스킬은 2026-07-17 에 추적 해제했지만 **커밋 이력에는 남아 있어 복구할 수 있습니다.** 마지막으로 스킬을 담은 커밋은 `ab3bf4f` 입니다. 새 환경에서는 아래로 복원한 뒤 link-skills 를 실행합니다.

```powershell
# 작업 트리에만 복원한다(--worktree). git 에 다시 추가되지 않는다.
git restore --source=ab3bf4f --worktree -- .agents/skills
# pwsh(PowerShell 7)가 있으면 `pwsh scripts/link-skills.ps1`. 없으면 아래로 실행한다.
powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1
```

## 작업 기록 (worklog)

작업 기록은 `.agents/worklog/`에 마크다운 한 건씩 쌓습니다. 나중에 사용자가 "그때 뭘 왜 했는지"를 되짚기 위한 기록입니다.

### 언제 쓰는가

기본 흐름은 `$issue-work` 스킬에 붙어 있습니다.

1. `$issue-work` 3단계에서 실행계획을 세워 사용자에게 보여준다.
2. **사용자가 검토하고 진행을 승인하면 그 시점에 worklog 파일을 만든다.** 승인받은 계획을 `## 실행 계획` 절에 그대로 적고 `status: wip` 로 둔다.
3. 4단계로 작업하며 계획에서 벗어난 것과 도중에 내린 판단을 같은 파일에 채운다.
4. 5단계 마무리에서 나머지 절을 완성하고 `status: done` 으로 바꾼다.

**승인 전에는 만들지 않습니다.** 계획이 반려되거나 바뀌면 기록할 내용도 달라집니다.

`$issue-work` 를 거치지 않은 작업이라도 코드를 바꿨으면 끝났을 때 같은 형식으로 남깁니다. 계획 단계가 없었으면 `## 실행 계획` 절은 생략합니다.

### 규칙

- Codex와 Claude가 **같은 폴더**를 함께 씁니다. 누가 했는지는 frontmatter 로 구분합니다.
- 이 폴더는 개인 영역이라 git 에 올리지 않습니다(`.gitignore`). 폴더가 없으면 만들어서 씁니다.
- 파일명은 `YYYY-MM-DD-작업슬러그.md` 입니다. 같은 날 여러 건이면 슬러그로 구분합니다.
- **`status: wip` 인 기록은 그 작업이 끝날 때까지 갱신합니다.** `done` 이 된 기록은 고치지 않고 새 파일로 쌓습니다. 이어지는 작업이면 `related` 로 이전 기록을 가리킵니다.
- 코드를 바꾸지 않은 작업(질문에 답만 한 경우, 오타 수정)은 남기지 않습니다.
- 토큰·비밀정보는 어떤 경우에도 적지 않습니다.

### 형식

````markdown
---
date: 2026-07-17
agent: claude            # 설계·구현을 한 에이전트 (claude | codex | "claude, codex")
model: opus-4.8          # 모르면 생략
committed_by: codex      # 커밋·PR 을 만든 에이전트($issue-pr). 아직이면 생략
branch: feat/#13
issue: "#13"             # 없으면 생략
status: wip              # wip | done
related: []              # 이어지는 작업이면 이전 기록 파일명
---

# 제목

## 배경
왜 이 작업이 필요했는지 한두 줄.

## 실행 계획
사용자가 승인한 계획을 그대로 적는다.

1. ...
2. ...

## 한 일
- 무엇을 어떻게 바꿨는지. **계획에서 벗어났으면 그 사실과 이유를 적는다.**

## 판단
- 갈림길에서 왜 그 쪽을 골랐는지. 코드만 봐서는 안 보이는 것만 적는다.

## 바꾼 파일
- `app/...`

## 남은 것
- 후속 작업이나 주의점. 없으면 "없음".
````

`판단` 절이 이 기록의 핵심입니다. 무엇을 바꿨는지는 git diff 가 이미 말해주므로, **왜 그렇게 했고 무엇을 버렸는지**를 남깁니다. `실행 계획`과 `한 일`을 함께 두는 이유도 같습니다 — 둘이 갈린 지점이 그 작업에서 실제로 배운 것입니다.

`agent` 와 `committed_by` 를 나눈 이유: 이 저장소에서는 한 에이전트와 설계·구현을 하고 커밋·PR 은 `$issue-pr` 로 다른 에이전트가 올리는 일이 흔합니다. 하나로 뭉뚱그리면 **어디서 실제 결정이 났는지**가 흐려집니다.

## Project Structure
```
app/
├── server.py                  # FastAPI 앱 생성 + 라우터 등록만 (얇게)
│
├── core/                      # 공통 인프라
│   ├── config.py              # 설정 (pydantic-settings, LLM_PROVIDER/API 키 등)
│   ├── logging.py             # 관찰 로그 설정            ← 체크7
│   └── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini 등, 확장형)
│
├── api/v1/
│   ├── router.py              # v1 라우터 취합
│   └── timeline.py            # POST /v1/timeline (taskId 접수 → 202), GET /{taskId}
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 입력 계약
│   ├── location.py/calendar.py/health.py/notification.py/photo.py  # 분리된 도메인 항목
│   ├── event_candidate.py     # AI 이벤트 후보 모델
│   ├── timeline_request.py    # 정규화된 요청(main agent 입력)
│   ├── repair.py              # Repair Agent 계약(문제 목록 + 도구 호출 계획)
│   └── timeline.py            # 타임라인 초안/이벤트 스키마
│
├── agents/                    # AI 에이전트
│   ├── base.py                # 공통 에이전트 인터페이스
│   ├── parsing.py             # LLM 호출/프롬프트/응답 파싱 유틸
│   ├── events/                # 데이터별 이벤트 에이전트 (source별 폴더)
│   │   ├── base_event_agent.py
│   │   └── __init__.py        # default_event_agents / merge_event_results
│   ├── timeline/timeline_agent.py   # 후보 → 초안 병합 (LLM 병합/파싱까지만)
│   ├── repair/                # 초안 검토·개선 (LLM 분석 + 도구 호출)
│   │   ├── repair_agent.py    # 확정 → 분석 → 도구 실행 → 재확정 반복(LangGraph)
│   │   ├── tools.py           # 도구 카탈로그: 서비스·상류 Agent 를 도구로 감싼다
│   │   └── prompt.md          # 분석·계획 system prompt
│   └── main/main_agent.py     # events → timeline → repair 조율(LangGraph)
│
└── services/
    ├── source_repository.py   # taskId로 수집 스냅샷 조회 (DB 추상화, 인메모리 스텁)
    ├── normalizer.py          # 수집 스냅샷을 itemType별로 분리·정규화
    ├── draft_repair.py        # draft 확정 repair (아래 순서대로 조립)
    ├── draft_edit.py          # event 수정·삭제 (Repair Agent 계획의 결정론 적용)
    ├── validator.py           # 요청 시간 범위(window) 강제: 범위 밖 event 제거/경고
    ├── source_lookup.py       # sourceRef → 입력 항목 역참조. rawId가 정식 식별자이고,
    │                          #   LLM이 붙인 sourceType 라벨은 입력의 실제 타입으로 정정한다
    ├── sleep_guard.py         # 수면 경계 강제: 기상 이전 event 제거, 수면에 걸친 event 클램프
    ├── stay_merge.py          # 이동 없이 이어진 같은 장소 STAY 묶기 (끊긴 수집 복원, 입력만 분석)
    ├── calendar_guard.py      # timeline 에서 통째로 빠진 캘린더 일정을 event 로 복원 (누락 방지)
    ├── calendar_location.py    # 캘린더 locationText ↔ STAY place/address 일치 시 confidence 보강
    ├── meal_guard.py           # MEAL event 지속시간 20~60분 강제 (긴 체류 전체를 식사로 잡지 않음)
    ├── place_resolver.py       # placeLabel을 근거 place로 확정, 근거 없는 address 제거
    ├── place_text.py           # 장소 문자열 정규화·비교 (calendar_location/place_resolver/stay_merge 공용)
    ├── task_store.py          # 처리 task 상태 저장 (PROCESSING/SUCCESS/FAILED)
    ├── timeline_runner.py     # 백그라운드: 조회→정규화→main agent→상태/콜백
    └── callback.py            # 완료 결과 App Server 콜백

# 처리 흐름: taskId 접수 → 202 즉시응답 → (백그라운드) DB 조회 → normalize → main agent → 상태 갱신/콜백
# main agent 그래프: run_event_agents → merge_results → run_timeline_agent → run_repair_agent
#   앞 3개는 LLM 이 의미를 판단하는 확률적 단계다. Repair Agent 는 그 둘이 섞여 있다.
#
# Repair Agent 한 번의 실행:
#   repair_draft(코드 확정) → 분석(LLM) → 도구 실행 → repair_draft(재확정) → 반복
#   반복은 done 이거나 settings.repair_max_iterations(기본 3)에서 멈춘다.
#   LLM 호출·파싱이 실패하면 마지막으로 확정된 draft 를 그대로 돌려주고 warning 을 남긴다.
#
# Repair Agent 도구: 결정론 서비스와 상류 Agent 를 그대로 감싼 것이다(로직 복제 없음).
#   조회   lookup_source
#   편집   update_event / delete_event            (services/draft_edit.py)
#   재적용 repair_durations / align_location_events / enforce_meal_duration /
#          enforce_sleep_boundary / resolve_places / ensure_calendar_events /
#          merge_stay_events / resolve_overlaps / reinforce_calendar_location
#   재실행 rerun_event_agent / rerun_timeline_agent
#   ※ 정렬·clientEventId 재부여·window 강제는 도구가 아니다. 결과가 반드시 일관돼야
#     하는 처리라 LLM 의 선택지로 두지 않고, 매 반복 끝의 repair_draft 가 항상 확정한다.
#
# repair_draft 순서: sourceType 정정 → 캘린더 복원 → duration → 근거 구간 정렬 → MEAL
#   → 수면 경계 → window → 장소 확정 → 정렬 → 체류 병합 → 겹침 정리 → confidence 보강
#   → clientEventId 재부여

tests/
├── agents/                    # Event/Repair Agent 테스트 (live 입력 테스트는 opt-in)
├── api/ · services/ · main/   # 엔드포인트·정규화·저장소·파이프라인 단위 테스트
├── integration/               # 실제 LLM 통합 테스트(opt-in)
└── fixtures/                  # 요청/스냅샷 빌더
```