# LLM Provider·프롬프트 계약

## Scope

LLM provider 선택·인증·vision/structured output·token 관측과 전역 prompt version 세트의 로딩 규칙을 설명한다.

## Read When

- OpenAI, Gemini, Bedrock provider를 변경하거나 추가할 때
- structured output parsing/retry를 바꿀 때
- Agent prompt나 `PROMPT_VERSION`을 바꿀 때
- LLM credential, model, token usage 관측을 수정할 때

## Authoritative Sources

- `app/core/config.py`, `app/core/llm.py`, `app/core/structured.py`
- `app/agents/prompt_loader.py`, `app/agents/**/prompts/**`
- 각 Agent 모듈의 `load_prompt` 호출과 prompt version 분기
- `tests/core/test_bedrock_provider.py`, `test_structured.py`, `test_structured_providers.py`, `test_llm_observation.py`
- `tests/agents/test_prompt_loader.py`, `test_prompt_sets.py`, `test_prompt_version_graph.py`

## Current Implementation

`LLM_PROVIDER`가 전역 provider를 고르고 `{PROVIDER}_MODEL`이 모델을 정한다. OpenAI와 Gemini는 각각 API key가 필요하며 값은 `app/core/secrets.py`의 `resolve_secret`으로 온다. 시크릿 번들이 `Settings`보다 우선하는 값 공급원이라 대부분 `settings` 필드로 채워지고, 설정 필드가 없는 키는 번들에서 직접 찾는다. provider는 출처를 모른다. Bedrock은 API key 필드 없이 boto3 credential chain을 사용하며 local에서는 optional profile, 배포에서는 EC2 instance role 또는 AgentCore execution role을 사용한다. 실제 값은 Knowledge나 Git에 기록하지 않는다.

세 provider 모두 text structured output과 vision input을 지원하는 구현으로 등록돼 있다. provider는 가능한 경우 native JSON schema/response schema/tool 형식을 사용한다. 자유형 object 때문에 strict schema 변환이 불가능하면 일반 JSON mode와 prompt schema hint로 내려가며 최종 검증은 항상 Pydantic이 수행한다.

`complete_json`은 JSON 형태를 요청하지만 tolerant item parsing처럼 호출자가 직접 검증할 때 쓴다. `complete_structured`는 공통 `run_structured`를 통해 Pydantic 모델을 검증하고 기본 한 번의 교정 retry를 수행한다. 첫 `{`부터 마지막 `}`까지 object를 추출하며 검증 실패 내용을 원래 prompt에 붙여 다시 요청한다. 모두 실패하면 `StructuredOutputError(1202)`다.

retry는 실패 종류에 따라 갈린다. **스키마 검증 실패**는 무엇이 틀렸는지 붙여 다시 묻고, **provider가 응답 자체를 내지 못한 실패**(`ProviderStructuredOutputError`)는 원본 prompt를 그대로 다시 보낸다. 지적할 직전 응답이 없는데 검증 실패 문구를 붙이면 사실이 아닌 지적이고 prompt만 길어진다. 두 예외 모두 코드는 1202다.

Bedrock은 Converse tool-use로 구조화 출력을 얻는다. 응답은 `stopReason`을 먼저 검사한다. Bedrock은 모델이 만든 tool call을 파싱하지 못하면 `stopReason=malformed_tool_use`와 **빈 content, 0 usage**를 담은 **HTTP 200**을 돌려주므로, 검사 없이 텍스트만 이어붙이면 빈 문자열이 정상 값처럼 흘러간다. `max_tokens`·`content_filtered`·강제했는데 도구를 부르지 않은 `end_turn`도 같은 자리에서 잡는다. 모델이 도구 대신 텍스트로 답했을 때만 그 텍스트를 넘긴다.

Bedrock 구조화 호출은 `temperature`를 0으로 잠그고 `inferenceConfig.maxTokens`를 항상 싣는다(`BEDROCK_MAX_TOKENS`, 기본 16384). 상한을 비우면 tool call 형식이 깨진다. `BedrockProvider`는 모델별 분기를 갖지 않으며 `BEDROCK_MODEL` 하나로 모델을 바꾼다. Nova 2 Lite는 `toolSpec.strict`와 `outputConfig.textFormat`을 지원하지 않으므로 제약 디코딩을 쓸 수 없고, 위 검증과 retry가 그 자리를 대신한다.

LLM call은 provider/model/version, duration, 사용 가능한 token bucket을 Langfuse generation에 기록한다. Langfuse가 꺼져 있으면 token 정보는 DEBUG 진단에만 남는다. provider SDK가 제공하지 않은 token 종류를 추측해 채우지 않는다.

`PROMPT_VERSION`은 현재 `v1` 또는 `v2`이고 모든 Agent가 같은 세트를 사용한다. loader는 module 옆 `prompts/{version}/{정확한 파일명}`만 UTF-8로 읽는다. version과 filename에 nested path를 허용하지 않으며, 파일이 없을 때 다른 version으로 fallback하지 않는다.

prompt 세트에는 현재 Timeline, Repair, Question, UserMemory, Calendar, Notification, Location, SleepActivity, Photo Agent가 실제 로드하는 파일이 모두 있어야 한다. v1 Location/Sleep은 review prompt를 사용하지만 v2는 단일 structured 호출이라 review 파일이 없어야 한다. Photo는 version마다 infer, metadata fallback, vision prompt가 필요하다.

UserMemory Agent(#64)는 Timeline pipeline 밖이지만 `PROMPT_VERSION`이 전역이라 `prompts/v1/prompt.md`와 `prompts/v2/prompt.md`를 모두 갖는다. 두 파일은 **같은 내용**이며 테스트가 동일성을 강제한다 — 이 Agent에는 되돌릴 v1 동작이 없어서, 갈라지면 rollback이 다른 동작을 만든다.

활성 prompt의 큰 의미 변경 전에는 같은 디렉터리에 version suffix 동결본을 둘 수 있다. loader는 활성 코드가 요청하는 정확한 filename만 읽으므로 동결본은 실행에 영향을 주지 않는다.

## Invariants

- provider 추가 시 Settings naming, registry, credential 방식, model, text/vision/structured/usage 계약을 함께 구현한다.
- provider native schema가 있어도 Pydantic 값·교차 검증을 생략하지 않는다.
- 모든 Agent는 하나의 `PROMPT_VERSION` 세트를 사용한다.
- prompt 누락을 조용히 v1로 fallback하지 않는다.
- key, AWS credential, token, 원본 provider error를 외부 response·운영 이벤트에 싣지 않는다.
- Timeline·Repair 최종 서술 규칙과 Event Agent 사실 보고 규칙을 섞지 않는다.
- UserMemory prompt는 문장 출처 구분(AI가 쓴 `title`/`subtitle`/`question` vs 사용자가 쓴 `memo`)을 명시한다. 이 지시가 빠지면 모델은 반드시 AI 문장에서 성향을 만들어 낸다.

## Known Gaps

- 지원 version이 Settings의 `Literal["v1", "v2"]`와 테스트 상수에 수동으로 중복돼 있다.
- 실제 provider 품질·비용·schema 준수는 opt-in live test 없이는 검증되지 않는다.
- provider model availability, 가격, service quota는 저장소 밖의 시점 의존 정보다.
- prompt 동결본 생성·메타데이터 기록을 자동화하는 도구는 없다.

## Update When

provider 목록·인증·기능, model 설정, structured 검증/retry, provider 응답 검증(`stopReason`)과 호출 파라미터, token usage 의미, prompt version·필수 파일·활성 파일·v1/v2 graph 차이가 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/core/test_bedrock_provider.py tests/core/test_structured.py tests/core/test_structured_providers.py tests/core/test_llm_observation.py -q`
- `uv run pytest tests/agents/test_prompt_loader.py tests/agents/test_prompt_sets.py tests/agents/test_prompt_version_graph.py -q`
- 실제 호출은 명시적으로 opt-in한 `live_llm` 테스트만 사용
- `rg -n "load_prompt\(|@register_provider|complete_structured|PROMPT_VERSION" app tests`

