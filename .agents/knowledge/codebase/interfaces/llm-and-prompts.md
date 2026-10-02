# LLM Provider·프롬프트 계약

## Scope

LLM provider 선택·인증·vision/structured output·token 관측과 전역 prompt version 세트의 로딩 규칙을 설명한다.

## Read When

- OpenAI, Gemini, Bedrock provider를 변경하거나 추가할 때
- structured output parsing/retry를 바꿀 때
- Agent prompt나 `PROMPT_VERSION`을 바꿀 때
- 단계별 모델 티어 배치나 `LLM_MODEL_*` 설정을 바꿀 때
- LLM credential, model, token usage 관측을 수정할 때

## Authoritative Sources

- `app/core/config.py`, `app/core/llm.py`, `app/core/llm_stages.py`, `app/core/structured.py`
- `app/agents/prompt_loader.py`, `app/agents/**/prompts/**`
- 각 Agent 모듈의 `load_prompt` 호출과 prompt version 분기
- `tests/core/test_bedrock_provider.py`, `test_structured.py`, `test_structured_providers.py`, `test_llm_observation.py`, `test_llm_stages.py`
- `tests/agents/test_prompt_loader.py`, `test_prompt_sets.py`, `test_prompt_version_graph.py`

## Current Implementation

`LLM_PROVIDER`가 전역 provider를 고르고 `{PROVIDER}_MODEL`이 기본 모델을 정한다. **모델은 단계별로 갈릴 수 있다**(#106) — `app/core/llm_stages.py`의 `LLMStage`(호출 지점 10개)와 `LLMTier`(`FAST`/`QUALITY`), 그리고 둘을 잇는 매핑표가 그 정본이다. 해석 순서는 **`{PROVIDER}_MODEL_{TIER}` → `{PROVIDER}_MODEL`** 이다. **티어 설정은 provider 별로 갈린다** — provider 마다 쓸 수 있는 모델이 달라(`gpt-5.4-mini` 는 OpenAI API 에만 있고 Bedrock 에는 없다) 공용 티어 하나로 두면 provider 를 바꾸는 순간 없는 모델 id 를 부른다. 현재 bedrock 과 openai 만 티어 필드를 갖고, gemini 는 언제나 `GEMINI_MODEL` 하나를 쓴다. 티어 설정이 비면 `default_llm()`이 model 없이 `LLMClient()`를 만들어 provider 싱글턴을 그대로 재사용한다. 그래서 티어를 하나도 넣지 않은 배포는 동작도 provider 인스턴스 수도 예전과 같다. **provider는 전역 하나로 유지한다** — 단계별 provider 선택은 없다. `get_provider`의 캐시 키가 `(name, model)`이라 인스턴스는 모델당 하나이고, 같은 티어 단계끼리 공유한다. 티어 이름은 모델 자체의 성질만 가리킨다(단계 배치는 바뀌는 값이라 이름에 용도를 담지 않는다). `LLMStage`는 설정 해석 전용이고 `ExecutionStage`(로그 상관키·Langfuse 계층)와 다른 값이다. `FAST`에 `photo_describe`가 들어 있으므로 **그 티어 모델은 vision을 지원해야 한다.** OpenAI와 Gemini는 각각 API key가 필요하며 값은 `app/core/secrets.py`의 `resolve_secret`으로 온다. 시크릿 번들이 `Settings`보다 우선하는 값 공급원이라 대부분 `settings` 필드로 채워지고, 설정 필드가 없는 키는 번들에서 직접 찾는다. provider는 출처를 모른다. Bedrock은 API key 필드 없이 boto3 credential chain을 사용하며 local에서는 optional profile, 배포에서는 EC2 instance role 또는 AgentCore execution role을 사용한다. 실제 값은 Knowledge나 Git에 기록하지 않는다.

세 provider 모두 text structured output과 vision input을 지원하는 구현으로 등록돼 있다. provider는 가능한 경우 native JSON schema/response schema/tool 형식을 사용한다. 자유형 object 때문에 strict schema 변환이 불가능하면 일반 JSON mode와 prompt schema hint로 내려가며 최종 검증은 항상 Pydantic이 수행한다.

`complete_json`은 JSON 형태를 요청하지만 tolerant item parsing처럼 호출자가 직접 검증할 때 쓴다. `complete_structured`는 공통 `run_structured`를 통해 Pydantic 모델을 검증하고 기본 한 번의 교정 retry를 수행한다. 첫 `{`부터 마지막 `}`까지 object를 추출하며 검증 실패 내용을 원래 prompt에 붙여 다시 요청한다. 모두 실패하면 `StructuredOutputError(1202)`다.

retry는 실패 종류에 따라 갈린다. **스키마 검증 실패**는 무엇이 틀렸는지 붙여 다시 묻고, **provider가 응답 자체를 내지 못한 실패**(`ProviderStructuredOutputError`)는 원본 prompt를 그대로 다시 보낸다. 지적할 직전 응답이 없는데 검증 실패 문구를 붙이면 사실이 아닌 지적이고 prompt만 길어진다. 두 예외 모두 코드는 1202다.

Bedrock은 Converse tool-use로 구조화 출력을 얻는다. 응답은 `stopReason`을 먼저 검사한다. Bedrock은 모델이 만든 tool call을 파싱하지 못하면 `stopReason=malformed_tool_use`와 **빈 content, 0 usage**를 담은 **HTTP 200**을 돌려주므로, 검사 없이 텍스트만 이어붙이면 빈 문자열이 정상 값처럼 흘러간다. `max_tokens`·`content_filtered`·강제했는데 도구를 부르지 않은 `end_turn`도 같은 자리에서 잡는다. 모델이 도구 대신 텍스트로 답했을 때만 그 텍스트를 넘긴다.

Bedrock 구조화 호출은 `temperature`를 0으로 잠그고 `inferenceConfig.maxTokens`를 항상 싣는다(`BEDROCK_MAX_TOKENS`, 기본 16384). 상한을 비우면 tool call 형식이 깨진다. `BedrockProvider`는 모델별 분기를 갖지 않으며 `BEDROCK_MODEL` 하나로 모델을 바꾼다. Nova 2 Lite는 `toolSpec.strict`와 `outputConfig.textFormat`을 지원하지 않으므로 제약 디코딩을 쓸 수 없고, 위 검증과 retry가 그 자리를 대신한다.

**`OpenAIProvider`는 Bedrock과 달리 모델별 요청 정책을 갖는다**(#108). `_MODEL_PARAMS` 표가 모델 id prefix로 `_OpenAIModelParams`를 고르고, 그 dataclass가 **매개변수마다 필드를 따로 둔다**(`reasoning_effort`, `accepts_temperature`). 두 값은 역할이 다르다 — 앞은 추론량이고 뒤는 샘플링 분산이라, 지금 쓰는 두 모델에서 함께 움직인다고 한쪽에서 다른 쪽을 파생시키면 서로 다른 축이 한 축으로 뭉개진다. 현재 `gpt-5.6-luna`는 `reasoning_effort="low"` + `accepts_temperature=False`, `gpt-5.4-mini`는 `"none"` + `True`다. 뒤 필드가 `False`인 이유는 GPT-5 계열 reasoning 모델이 추론이 켜진 상태에서 `temperature`를 받지 않고 기본값 1로 고정하기 때문이다(실측 400: `Only the default (1) value is supported`). 따로 선언하면 표 안에서 어긋날 수 있으므로 **테스트가 표의 자기 일관성을 검사한다**(추론이 켜진 항목은 `accepts_temperature=True`일 수 없다). 모델 기본값과 같은 `none`을 굳이 명시하는 이유는 OpenAI가 기본값을 바꾸면 고른 적 없는 추론이 켜지면서 `temperature`가 조용히 거부되기 때문이다. prefix로 재는 이유는 날짜 스냅샷 id(`gpt-5.6-luna-2026-xx-xx`)를 놓치지 않기 위해서다. **표에 없는 모델은 `_DEFAULT_MODEL_PARAMS`를 써서 `reasoning_effort`를 싣지 않고 `temperature`를 그대로 보낸다** — 추론을 지원하지 않는 모델에 설정을 밀어넣지 않기 위해서다. 호출자가 kwargs로 같은 키를 직접 주면 그쪽이 이긴다.

이 표는 **설정이 아니라 코드가 소유한다.** 모델이 어떤 매개변수를 받는지는 배포 환경이 아니라 모델 자체의 성질이라, 환경변수로 빼면 환경마다 다르게 틀릴 수 있다. Agent 호출부의 `temperature` 인자는 바뀌지 않았고 provider 경계에서만 걸러진다. Langfuse generation의 `model_parameters`에는 **실제 request에 실린 값만** 담는다 — 요청에서 뺀 `temperature`가 남으면 적용되지 않은 값이 실효값처럼 보인다.

LLM call은 provider/model/version, duration, 사용 가능한 token bucket을 Langfuse generation에 기록한다. Langfuse가 꺼져 있으면 token 정보는 DEBUG 진단에만 남는다. provider SDK가 제공하지 않은 token 종류를 추측해 채우지 않는다.

`PROMPT_VERSION`은 현재 `v1`·`v2`·`v3` 중 하나이고 모든 Agent가 같은 세트를 사용한다. loader는 module 옆 `prompts/{version}/{정확한 파일명}`만 UTF-8로 읽는다. version과 filename에 nested path를 허용하지 않으며, 파일이 없을 때 다른 version으로 fallback하지 않는다. v3은 v2의 활성 파일을 복사해 시작한 세트다(#112·#114). v2 디렉터리의 동결본(`timeline_v2.0.0.md` 등)은 v2의 이력이라 v3으로 옮기지 않았다.

Timeline Agent가 provider에 싣는 구조화 출력 스키마는 내부 draft가 아니라 `TimelineAgentOutput`(`events`·`warnings`)이다(#118). 내부 모호성 질문의 자유형 `timeRange` dict가 없어져 OpenAI 경로는 strict schema로 잠글 수 있다. 이 계약은 버전을 가리지 않는다 — v2 프롬프트가 여전히 `questions`·`userId`를 내라고 하지만 parse가 무시한다. v3 Timeline·Question 프롬프트는 #118에서 v2와 갈라졌다(판단 순서대로 읽히는 여섯 단계, eventType별 절·예시, User Memory 별도 단계, Question eventType별 예시, 수면 미처리). Timeline·Repair v3는 #134에서 수면을 다시 다룬다 — 출력 eventType은 13종 전부이고 Question v3만 수면 예시가 없다. v3 Timeline은 말투 규정을 v2처럼 입력 절 앞에 두지 않고 문장을 쓰는 5단계에 둔다 — `tests/agents/test_prompt_sets.py`가 버전별 자리를 고정한다. v3 Timeline은 User Memory 필드마다 뜻을 한 줄씩 적는다. 정본은 User Memory Agent 프롬프트의 필드 정의 표이고 두 곳의 문장이 같아야 한다. `tests/agents/test_timeline_v3_prompt.py`가 그 계약과 v2 무변경을 고정한다.

prompt 세트에는 현재 Timeline, Repair, Question, UserMemory, Calendar, Notification, Location, SleepActivity, Photo Agent가 실제 로드하는 파일이 모두 있어야 한다. v1 Location/Sleep은 review prompt를 사용하지만 v2 이후는 단일 structured 호출이라 review 파일이 없어야 한다. Photo는 version마다 infer, metadata fallback, vision prompt가 필요하다.

**Event Agent 출력 계약(`AiEventCandidate`)은 버전을 가리지 않고 하나다.** 필드를 빼면 옛 세트에도 곧바로 적용된다. #114에서 `evidenceSummary`·`semanticTags`를 뺐고 v1·v2 문구는 그대로 두었으므로, v1·v2 prompt는 여전히 두 필드를 내라고 하지만 모델이 내더라도 모르는 키로 버려지고 candidate는 살아남는다. Location의 `derivedMetrics`도 코드 하나가 모든 버전에 싣는다 — v2 prompt가 설명하는 `transports`·`transportConflict`는 더 이상 없고 `mode`·`modeConflict`가 그 자리를 대신한다. Notification 입력도 같다(#116). 코드는 버전을 가리지 않고 `policies`·`notifications`·`conversations`를 싣고, v1·v2 prompt가 설명하는 `appDictionary`·`appPolicy`·`timelineUseGuidance`·`messengerAnalysis`는 더 이상 오지 않는다.

UserMemory Agent(#64)는 Timeline pipeline 밖이지만 `PROMPT_VERSION`이 전역이라 버전마다 `prompt.md`를 갖는다. v1과 v2는 **같은 내용**이며 테스트가 동일성을 강제한다. v3는 #121에서 갈라졌다 — AI가 쓴 `title`·`subtitle`도 근거로 읽고, 폭넓게 모으고, 한 번 나온 정보도 남기고, 하루 감정을 반영한다. 프로필 문장은 음슴체로 짧게 쓴다. 마침표를 찍으라고 하지 않는다 — 코드는 문장을 세지 않고, 줄일 몫은 글자 수로, 지우기만 한 변경은 어절로 본다. 프로필을 읽는 Timeline·Repair v3 prompt는 `한 번 함`·`~로 보임` 표지를 예전 말투(`한 번 했습니다`·`~로 보입니다`)와 함께 알아본다 — 쓰는 쪽의 표지를 바꾸면 읽는 쪽 문장도 같이 고친다. 갱신 요청에 붙는 `[근거 없음]` 지시(`memo` 없는 날 성향 필드를 그대로 두라는 것)는 코드에 있어 버전으로 가른다(`_MEMO_ONLY_TRAITS`). v1·v2에서만 붙고, `PROMPT_VERSION`을 되돌리면 지시도 함께 돌아온다.

**모델 출력의 모양도 세트로 갈린다**(#121, `_PATCH_OUTPUT`). v3는 구조화 출력 스키마가 `UserMemoryPatch`다 — 변경 목록(`changes`)을 받고 Agent가 기존 문서에 끼워 넣는다. 변경 한 건은 `item`·`action`·`reason`·`text` 순서이고, `reason`은 모델이 `text`를 쓰기 전에 적는 판단 과정이라 코드는 읽지 않는다(Repair의 `toolCalls[].reason`과 같은 자리). 추론이 꺼진 모델이 이유 없이 변경만 낼 때 같은 사실을 여러 항목에 적고 상관없는 속성에 몰아 적던 것을 줄이려고 둔 자리다. **LLM 출력 스키마의 필드 선언 순서가 곧 모델이 쓰는 순서다.** v1·v2는 `UserMemory` 문서 전체를 받는다. 어느 쪽이든 Agent의 반환값은 문서 전체라 호출부는 세트를 모른다. 두 분기값(`_MEMO_ONLY_TRAITS`, `_PATCH_OUTPUT`)은 #119가 둔 기준 `uses_legacy_contract()` 하나에서 나온다. `UserMemory`는 `customAttributes`가 자유형 dict라 provider의 strict 스키마로 표현되지 않아 JSON 모드로 받지만, 변경 목록은 (항목, 동작, 문장)의 목록이라 strict로 강제된다. **LLM 출력 스키마의 클래스 docstring은 JSON schema의 `description`으로 provider에 나간다** — 설계 이유는 docstring이 아니라 주석에 적는다.

UserMemory 갱신 요청(user prompt)은 `[existing user memory]`, `[dailyTimelines]`, `[크기]`, (v1·v2만) `[근거 없음]`, 그리고 출력의 모양을 말하는 마지막 문장(v3는 "바꿀 항목만 `changes`에, 변경마다 `reason`을 먼저", v1·v2는 "User Memory 전체") 순서로 조립한다. `[크기]`는 기존 프로필의 크기·목표(4,500자)·상한(5,000자)을 알리고, 기존 프로필이 목표를 넘었으면 긴 항목부터 골라 몇 자까지 줄일지를 함께 준다. 이 조립은 버전을 가리지 않는다. **갱신 한 건은 LLM 호출 한 번이다** — 규칙을 어긴 갱신본을 다시 요청하지 않는다(#121). 지적을 붙여 다시 묻던 경로는 걷어냈다. 스키마 검증 실패 시의 구조화 출력 교정 재시도는 모든 Agent가 쓰는 별개의 장치이고 그대로다. 다만 v3의 변경 목록은 **변경 한 건의 잘못(없는 항목, 빈 문장, 항목 길이 초과)을 스키마 검증 오류로 두지 않는다** — 그 변경만 빼고 적용하므로 교정 재시도로 가지 않는다. 교정 재시도로 가는 것은 JSON이 깨졌거나 모르는 키·동작이 있을 때뿐이다.

UserMemory의 digest와 schema 상한은 버전을 가리지 않는다. 하루 감정(`emotion`)과 끝 시각(`endHour`)은 v1·v2 입력에도 실리며, 그 세트의 prompt는 두 키를 설명하지 않는다. v3 prompt가 말하는 상한(필드·값 500자, 전체 5,000자)과 목표(4,500자), 감정 다섯 값, 입력 키, 동작 세 가지(`추가`·`수정`·`삭제`)는 코드와 같아야 하고, 출력 예시는 `UserMemoryPatch`로 검증돼야 하며 `tests/agents/test_user_memory_agent.py`가 고정한다. 같은 테스트가 v3 prompt의 구조도 본다 — 제품 비전 없이 역할(어떤 사람으로서 기록을 읽는지와 일하는 태도)로 시작하고 그다음에 무엇을 내는지를 말하는지, 최상위 절이 작업 순서(기록 읽기 → 항목별 추론 → 변경 결정 → 문장 작성 → 최종 검증)대로 놓였는지, 고정 필드 열 개와 `customAttributes`가 2단계 안의 각자의 절에 담는 것·읽는 근거·추론·갱신·근거가 약할 때·예시·피할 문장을 갖는지, **단계의 규칙이 그것을 쓰는 단계 한 곳에만 적혀 있는지**다. 모델이 어긴 규칙을 다른 절에도 덧붙이면 prompt가 불어나므로(7천 자 → 2만 7천 자) 새 규칙은 그것을 쓰는 단계나 그 항목의 절 한 곳에 적는다. 필드의 정의는 그 절의 **담는 것** 한 줄이고 Timeline·Repair v3가 같은 문장으로 읽는다. 크기 위반 지적은 줄이는 순서를 말하지 않고 prompt의 「크기」 절을 가리키므로 **모든 세트가 그 절을 가져야 한다.**

활성 prompt의 큰 의미 변경 전에는 같은 디렉터리에 version suffix 동결본을 둘 수 있다. loader는 활성 코드가 요청하는 정확한 filename만 읽으므로 동결본은 실행에 영향을 주지 않는다.

## Invariants

- provider 추가 시 Settings naming, registry, credential 방식, model, text/vision/structured/usage 계약을 함께 구현한다.
- OpenAI 모델별 요청 매개변수는 모델마다 필드를 따로 선언한다. 한 값에서 다른 값을 파생시키지 않고, 어긋남은 표 일관성 테스트가 잡는다.
- 모델 요청 정책을 환경변수로 옮기지 않는다. 모델의 성질이지 환경의 선택이 아니다.
- 관측 `model_parameters`에는 실제 request에 실린 값만 남긴다. 적용되지 않은 호출자 값을 싣지 않는다.
- provider native schema가 있어도 Pydantic 값·교차 검증을 생략하지 않는다.
- 모든 Agent는 하나의 `PROMPT_VERSION` 세트를 사용한다.
- prompt 누락을 조용히 v1로 fallback하지 않는다.
- key, AWS credential, token, 원본 provider error를 외부 response·운영 이벤트에 싣지 않는다.
- Timeline·Repair 최종 서술 규칙과 Event Agent 사실 보고 규칙을 섞지 않는다.
- Repair가 보는 warning·입력·도구는 prompt 세트에 따라 갈린다(#119). v3는 확정 pass의 새 검사가 남긴 warning, 확정 pass의 기록·event별 근거·User Memory, `split_event`를 받고, v1·v2는 예전 그대로다. 갈리는 기준은 `prompt_loader.uses_legacy_contract` 하나이며(`v1`·`v2`가 예전 계약) 문자열 대소 비교를 쓰지 않는다. 세트가 설명하지 않는 warning·입력·도구를 주지 않는다. 새 세트를 더하면 이름을 `LEGACY_PROMPT_VERSIONS`에 넣지 않는 한 새 계약으로 돈다.
- 모든 `LLMStage`는 티어 배치를 갖는다. 배치가 빠진 단계를 조용히 전역 모델로 흘려보내지 않는다.
- `photo_describe`가 속한 티어의 모델은 vision을 지원해야 한다. 이미지 입력을 쓰는 유일한 단계다.
- 티어 이름은 모델의 성질만 가리킨다. 단계 배치는 바뀌는 값이므로 이름에 용도를 담지 않는다.
- 티어 설정은 provider 별이다. 한 provider 의 티어 값이 다른 provider 에 새지 않는다.
- UserMemory prompt는 세트를 가리지 않고 문장 출처(AI가 쓴 `title`/`subtitle` vs 사용자가 남긴 `memo`·감정)를 명시한다. v1·v2에서 이 지시가 빠지면 모델은 AI 문장에서 성향을 만들어 내고, v3에서 빠지면 문장의 말투를 성격으로 옮겨 적는다.
- UserMemory의 두 근거 정책을 한 prompt에 섞지 않는다. 옛 규칙이 한 줄이라도 남으면 모델은 둘 중 보수적인 쪽을 고른다.

## Known Gaps

- 지원 version이 Settings의 `Literal["v1", "v2", "v3"]`와 테스트 상수에 수동으로 중복돼 있다.
- gemini에는 티어 필드가 없다. `GEMINI_MODEL_FAST` 같은 값을 넣어도 `extra="ignore"`로 조용히 무시된다.
- 실제 provider 품질·비용·schema 준수는 opt-in live test 없이는 검증되지 않는다.
- provider model availability, 가격, service quota는 저장소 밖의 시점 의존 정보다.
- prompt 동결본 생성·메타데이터 기록을 자동화하는 도구는 없다.

## Update When

provider 목록·인증·기능, model 설정과 단계별 티어 배치·해석 순서, structured 검증/retry, provider 응답 검증(`stopReason`)과 호출 파라미터, token usage 의미, prompt version·필수 파일·활성 파일·v1/v2 graph 차이가 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/core/test_bedrock_provider.py tests/core/test_structured.py tests/core/test_structured_providers.py tests/core/test_llm_observation.py tests/core/test_llm_stages.py -q`
- `uv run pytest tests/agents/test_prompt_loader.py tests/agents/test_prompt_sets.py tests/agents/test_prompt_version_graph.py -q`
- 실제 호출은 명시적으로 opt-in한 `live_llm` 테스트만 사용
- `rg -n "load_prompt\(|@register_provider|complete_structured|PROMPT_VERSION" app tests`

