# 구현·운영 제약

## Scope

코드 변경 시 선택사항처럼 보이지만 현재 계약과 운영 방식 때문에 반드시 지켜야 하는 제약을 모은다.

## Read When

- worker 수, storage, 인증, 로그, prompt, 결과 순서를 바꿀 때
- 새 dependency·외부 sink·endpoint를 추가할 때
- 실패 처리나 retry를 공통화·리팩터링할 때

## Authoritative Sources

- `app/core/config.py`, `app/core/error_codes.py`, `app/core/operational_logging.py`, `app/core/redaction.py`
- `app/services/app_server_client.py`, `app/services/timeline_runner.py`, `app/services/draft_repair.py`
- `Dockerfile`, `.dockerignore`, `scripts/deploy-ec2.sh`
- `tests/core/test_no_direct_elasticsearch.py`, `tests/core/test_logging.py`, `tests/services/test_timeline_runner.py`

## Current Implementation

### 상태와 데이터 경계

- AI 서버는 제품 DB에 직접 접근하지 않는다. 입력 조회와 결과 저장은 App Server API만 사용한다.
- task 상태를 저장하거나 조회 endpoint로 제공하지 않는다. callback은 상태 통보일 뿐 결과 데이터를 싣지 않는다.
- 앱 코드에 Elasticsearch URL·API key·`_bulk` 호출을 추가하지 않는다. 운영 이벤트 전달은 Filebeat 책임이다.

### 실행과 배포

- Uvicorn worker를 늘리지 않는다. inflight counter가 프로세스 로컬이며 `/ping`의 `HealthyBusy` 판단과 EC2 idle 대기가 이 값에 의존한다.
- 202 응답은 처리 완료가 아니다. HTTP latency와 background task duration을 같은 지표로 합치지 않는다.
- 컨테이너는 non-root, 8080, deny-all Docker build context를 유지한다. 환경별 secret과 URL을 이미지에 굽지 않는다.

### App Server 순서와 인증

- `taskToken`은 `Task-Token` header에만 넣고 URL·request body·로그·trace에 넣지 않는다.
- 성공 응답 body의 새 `taskToken`만 흡수하며 다음 호출부터 같은 holder의 최신 값으로 보낸다.
- timeout과 5xx만 같은 body·token으로 retry한다. 401/404/409는 callback 없이 중단한다.
- 결과 저장 성공을 확인하기 전에는 SUCCESS callback을 보내지 않는다.
- 결과 저장 성공 뒤 callback 실패가 나도 task 결과를 FAILED로 되돌리지 않는다.

### 오류·로그·관측

- 외부 실패는 `ErrorCode` 정수와 카탈로그 안전 메시지를 쓴다. 원본 예외 문자열은 API·callback에 넣지 않는다.
- Elasticsearch 수집 대상 운영 이벤트는 `emit_event`가 붙인 `event.dataset=laimory.api` 표식과 이벤트별 allowlist로 제한한다.
- Langfuse 실패와 운영 로그 실패는 주 처리를 실패시키지 않는다.
- secret·개인정보·사용자 본문은 적재 대상으로 추가하지 않는다.

### Agent와 prompt

- `PROMPT_VERSION`은 Agent별 선택이 아니라 전체 prompt 세트를 고른다. 지원하지 않거나 파일이 빠진 version을 v1으로 fallback하지 않는다.
- 활성 prompt는 코드가 정확한 파일명으로 로드한다. `_vX.Y.Z.md` 동결본은 런타임 입력이 아니다.
- source 식별자는 입력의 `rawId`다. 내부 ID나 LLM이 만든 ID로 fallback하지 않는다.
- Repair 결정론 pass는 최소 한 번 실행하며 정렬, window, source, `clientEventId` 같은 규칙을 LLM 선택에 맡기지 않는다.

## Invariants

위 항목 전체가 불변식이다. 특히 상태·데이터·token·저장/callback 순서 제약은 서로 결합돼 있어 한 항목만 완화하면 실패 분류와 운영 교체 안전성이 함께 깨진다.

## Known Gaps

- inbound `/v1/timeline`, `/invocations`, `/health`, `/debug/env`에 애플리케이션 수준 인증·인가 계층이 없다. 네트워크 또는 상위 런타임에서 제한하는지는 이 저장소만으로 확인할 수 없다.
- `/debug/env`는 key 값이 아니라 존재 여부만 반환하지만, 운영 비활성화나 인증 제한 코드가 없다.
- photo URL은 HTTP(S) 형식·크기·MIME·timeout을 검사하지만 hostname allowlist가 없다. 테스트가 이를 현재 계약으로 명시한다.

## Update When

금지 사항, 단일 worker, 상태·데이터 소유, token·retry·callback 순서, 관측 수집 경계, prompt 세트 규칙이 추가·변경·해제될 때 갱신한다.

## Validation

- `uv run pytest tests/core/test_no_direct_elasticsearch.py tests/core/test_logging.py tests/core/test_operational_logging.py -q`
- `uv run pytest tests/services/test_app_server_client.py tests/services/test_timeline_runner.py -q`
- `uv run pytest tests/agents/test_prompt_loader.py tests/agents/test_prompt_sets.py -q`
- `rg -n "workers|Task-Token|event.dataset|_bulk|prompt_version" app Dockerfile tests`

