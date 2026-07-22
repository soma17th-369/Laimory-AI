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

## Project Structure
```
app/
├── server.py                  # FastAPI 앱 생성 + 라우터 등록만 (얇게)
│
├── core/                      # 공통 인프라
│   ├── config.py              # 설정 (pydantic-settings, LLM_PROVIDER/API 키, DB_*, OBS_*/ES_* 등)
│   ├── logging.py             # 운영 로그 설정 (rich | stdout JSON→CloudWatch, LOG_FORMAT)
│   ├── llm.py                 # LLM provider 래퍼 (OpenAI/Gemini/Bedrock, 확장형) + LLM 관측/토큰 emit
│   ├── db.py                  # staging MySQL async engine/session (aiomysql, host/port 직결)
│   ├── db_models.py           # staging 테이블 ORM 매핑 (draft source / daily record / timeline event·item / event↔item N:M 조인)
│   └── observability/         # Timeline 실행 관측 (#28). taskId 단일 키, 본문 비저장·메타데이터 제한
│       ├── models.py          #   ObservationEvent 계약 (taskId/sequence/stage/token/version)
│       ├── context.py         #   contextvars 로 to_thread 까지 taskId 전파, emit_observation
│       ├── observer.py        #   요청별 Observer: sequence 부여·마스킹·sink 실패 격리
│       ├── redaction.py       #   본문 비저장·payload 메타데이터 마스킹/크기 제한
│       ├── sinks.py           #   Null/InMemory(버퍼)/JsonLines/Composite (제품 독립)
│       ├── documents.py       #   이벤트 버퍼 → event 문서 N건(FINAL에 task 집계 포함)
│       ├── elasticsearch.py   #   httpx NDJSON _bulk 전송 (재시도/부분실패/완전격리)
│       └── runtime.py         #   요청별 Observer/buffer 생성 + flush(로컬 + ES)
│
├── api/v1/
│   ├── router.py              # v1 라우터 취합
│   └── timeline.py            # POST /v1/timeline (taskId+callbackToken+dailyRecordId+window 접수 → 202). 상태 조회 없음(상태는 App Server 소유)
│
├── schemas/                   # Pydantic 계약(contract)
│   ├── source_snapshot.py     # 수집 원본(taskId/sourceItems) 입력 계약
│   ├── location.py/calendar.py/health.py/notification.py/photo.py  # 분리된 도메인 항목
│   ├── event_candidate.py     # AI 이벤트 후보 모델
│   ├── timeline_request.py    # 정규화된 요청(main agent 입력)
│   └── timeline.py            # 타임라인 초안/이벤트 스키마
│
├── agents/                    # AI 에이전트
│   ├── base.py                # 공통 에이전트 인터페이스
│   ├── parsing.py             # LLM 호출/프롬프트/응답 파싱 유틸
│   ├── events/                # 데이터별 이벤트 에이전트 (source별 폴더)
│   │   └── base_event_agent.py
│   ├── timeline/timeline_agent.py   # 후보 → 초안 병합 (LLM 병합/파싱까지만)
│   └── main/main_agent.py     # events → timeline → repair 조율(LangGraph)
│
└── services/
    ├── source_repository.py   # taskId로 수집 스냅샷 조회 (MySQL: timeline_draft_source_items / 인메모리 스텁)
    ├── timeline_repository.py # 결과 저장: timeline_events(요청 dailyRecordId 로 FK) + timeline_items(저장 시 raw_id 디듀프, daily record 소속은 event 만) + timeline_event_items(event↔item N:M). AI 생성분만 교체하는 트랜잭션
    ├── timeline_validator.py  # 저장 전 자체검증 (task source 소속/시간 등)
    ├── normalizer.py          # 수집 스냅샷을 itemType별로 분리·정규화
    ├── draft_repair.py        # draft 확정 repair (아래 순서대로 조립)
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
    ├── timeline_runner.py     # 백그라운드(무상태): 조회→정규화→main agent→staging 저장→콜백. 최종 상태 반환
    └── callback.py            # 완료 통보(SUCCESS/FAILED + callbackToken) App Server 콜백

# 처리 흐름: taskId+callbackToken+dailyRecordId+window 접수 → 202 즉시응답 →
#   (백그라운드) DB 조회 → 요청 window 를 정본으로 덮어쓰기 → normalize → main agent
#   → timeline_events(dailyRecordId FK)/timeline_items(저장 시 디듀프)/timeline_event_items(N:M) 저장
#   → 콜백(SUCCESS/FAILED 통보만; 실제 결과는 App Server 가 staging DB 에서 읽음)
# AI 서버는 무상태다. task 상태는 App Server 가 소유하며(AI 는 상태 저장/조회 없음),
#   AI 는 상태를 콜백으로만 통보한다. daily_records 도 직접 조회/생성하지 않고 dailyRecordId 로 FK 만 건다.
# 저장/조회는 항상 실제 staging DB. DB 는 필수이며(없으면 실패), 인메모리 스텁은 단위 테스트 전용.
# 접속 스모크: scripts/db_smoke.py (SSH 터널 열고 .env 채운 뒤 실행)
# main agent 그래프: run_event_agents → merge_results → run_timeline_agent → repair_draft
#   앞 3개는 LLM 이 의미를 판단하는 확률적 단계, repair_draft 는 코드가 확정하는 결정론적 단계다.
# repair_draft 순서: sourceType 정정 → 캘린더 복원 → duration → 근거 구간 정렬 → MEAL
#   → 수면 경계 → window → 장소 확정 → 정렬 → 체류 병합 → 겹침 정리 → confidence 보강
#   → clientEventId 재부여

tests/
├── agents/                    # Event Agent live 입력 테스트(opt-in)
├── api/ · services/ · main/   # 엔드포인트·정규화·저장소·파이프라인 단위 테스트
├── integration/               # 실제 LLM·staging MySQL 통합 테스트(opt-in)
└── fixtures/                  # 요청/스냅샷 빌더
```
