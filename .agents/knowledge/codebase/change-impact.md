# 변경 영향 가이드

## Scope

자주 반복되는 변경 유형이 어떤 코드 경계·계약·테스트·Knowledge 문서에 영향을 주는지 연결한다. 모든 파일 목록이 아니라 의미 변화의 동반 검토 지점이다.

## Read When

- 구현 범위와 회귀 테스트를 정할 때
- 한 계층 변경이 외부 계약·운영에 미치는 영향을 놓치지 않으려 할 때
- 코드 수정 뒤 어떤 Knowledge 문서가 실제로 갱신 대상인지 판단할 때

## Authoritative Sources

- `rg`로 확인한 실제 import·call 관계
- `app/**` 구현과 `tests/**`의 대응 테스트
- `.github/workflows/**`, `Dockerfile`, `scripts/**`

## Current Implementation

| 변경 유형 | 함께 검토할 경계 | 최소 검증 | 의미가 바뀌면 갱신할 Knowledge |
|---|---|---|---|
| inbound Timeline 요청·응답 | AgentCore adapter, 공통 오류 응답, 요청 로그의 taskId | Timeline/AgentCore/error/request logging API 테스트 | HTTP API, Timeline task |
| App Server path/body/status | client 모델, token holder, runner 실패·callback 순서 | App Server client, runner, result 변환 테스트 | App Server 계약, 데이터, Timeline task |
| 새 source `itemType` | snapshot enum, domain schema, normalizer, Event Agent, source 무결성 | normalizer, source contract/integrity, Agent 테스트 | 데이터, Agent pipeline, 공통 언어·불변식 |
| Timeline event 필드 | draft schema, prompt/parse, Repair 편집, result mapper, 저장 검증 | timeline/repair/result/validator 테스트 | 데이터, Agent pipeline, App Server 계약 |
| guard 또는 Repair 순서 | warning 재계산, 병합·정렬·ID, Repair tool catalog | 해당 guard, draft repair, Repair Agent 테스트 | Agent pipeline, 불변식 |
| ErrorCode | 카탈로그 메시지·HTTP mapping, 예외/runner, callback, docs 표 | error code/handler/runner 테스트 | 제약, 관측성, 관련 interface |
| LLM provider | Settings 이름, provider registry·인증, structured/vision/usage, Langfuse metadata | provider·structured·token 관측 테스트 | LLM·프롬프트, 관측성, 로컬 개발 |
| prompt version·활성 prompt | 모든 Agent 필수 파일, Settings Literal, loader, 동결본 | prompt loader/sets/version graph 테스트 | LLM·프롬프트, Agent pipeline, 불변식 |
| 운영 이벤트 | enum, allowlist, emitter 호출부, JSON formatter, Filebeat mapping | operational/request logging, runner/client, Filebeat 테스트 | 관측성, 제약 |
| 배포 환경변수 | Settings 필수·기본값, Docker default, EC2 runtime.env/AgentCore 보존 | config, server import, script 테스트 | 로컬 개발, 배포, 관련 interface |
| worker/background 방식 | inflight, `/ping`, EC2 idle wait, AgentCore lifecycle | AgentCore endpoint, runner, deploy script | 아키텍처, Timeline task, 배포, 제약 |

문서를 갱신할지는 마지막 열만으로 결정하지 않는다. 해당 페이지 Router의 `Update when` 조건과 실제 의미 변화가 모두 있어야 한다.

## Invariants

- schema 변경은 producer와 consumer 양쪽을 함께 검토한다.
- 외부 계약 변경은 성공 경로뿐 아니라 retry, abort, safe error, 관측 필드도 검토한다.
- Repair/guard 변경은 호출 순서와 반복 시 warning stale 여부를 함께 검토한다.
- 문서만 맞추기 위해 코드를 추측하지 않는다.

## Known Gaps

- 정적 type checker나 import-layer linter 설정은 없다. 계층 영향은 `rg`, 테스트, 코드 리뷰로 확인한다.
- 자동 문서 링크 검사나 Router freshness CI는 아직 없다.

## Update When

새로운 반복 변경 단위가 생기거나 기존 변경 유형의 실제 영향 경계·최소 회귀 테스트가 달라질 때만 갱신한다.

## Validation

- `rg -n "from app\.|import app\." app tests`
- 수정 symbol에 대해 `rg -n "<symbol>" app tests docs .agents/knowledge`
- 표의 최소 검증을 대상 변경에 맞춰 실행

