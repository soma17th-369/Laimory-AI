# 시크릿 번들 운영 매뉴얼

> 기준일: 2026-08-24
> 대상: Laimory AI 서버의 설정·시크릿을 AWS Secrets Manager로 옮기고 운영하는 절차 (이슈 #30)
> 이 문서는 **AWS 웹 콘솔** 기준이다. CLI 절차는 [AgentCore 배포 가이드](deploy-agentcore.md)를 참고한다.

## 1. 값을 어디에 두나

| 종류 | 두는 곳 | 예 |
|---|---|---|
| 비밀, 환경마다 달라지는 값 | **Secrets Manager 시크릿 번들** | `OPENAI_API_KEY`, `LANGFUSE_SECRET_KEY`, `APP_SERVER_API_URL`, `BEDROCK_MODEL` |
| 부트스트랩·배포 주입값 | EC2 `runtime.env` 또는 AgentCore 환경 변수 | `APP_ENV`, `SECRETS_BUNDLE_NAME`, `AGENT_VERSION` |
| 그 밖의 고정값 | `app/core/config.py` 기본값 (코드) | 타임아웃, 재시도 횟수, 사진 다운로드 상한 |

환경별 구성은 다음과 같다.

- **dev** — EC2 컨테이너. `runtime.env` + 시크릿 번들
- **prod** — AgentCore Runtime. 환경 변수 + 시크릿 번들
- **로컬** — `.env`만. `SECRETS_BUNDLE_NAME`이 없으면 **AWS를 호출하지 않는다**

## 2. 우선순위와 실패 규칙

```text
시크릿 번들  >  환경변수 / .env  >  config.py 기본값
```

- **번들이 이긴다.** `runtime.env`에 옛 값이 남아 있어도 번들 값이 적용되므로, 옮긴 뒤 파일을
  정리하지 않아도 동작이 달라지지 않는다(정리는 권장한다).
- 번들에 **없는** 키는 그대로 환경변수 → `.env` → 코드 기본값으로 내려간다. 그래서 번들에는
  넣을 것만 넣으면 된다.
- 번들은 **기동 시 1회** 읽는다. 값을 바꾸면 컨테이너를 재시작해야 반영된다.
- 조회에 실패하면 그 값들만 없는 것으로 보고 아래 단계로 내려간다. 실패는 오류코드 `1408`로
  로그에 남는다.
  ⚠️ 다만 **필수 설정(`APP_SERVER_API_URL` 등)을 번들에만 두었다면 조회 실패는 곧 기동
  실패**다. 그 값이 어디에도 없기 때문이다.
- `SECRETS_BUNDLE_NAME`은 번들에서 올 수 없다. 어느 번들을 읽을지 정하는 값이라 환경변수나
  `.env`로만 온다.
- `AGENT_VERSION`은 번들에 넣지 않는다. 배포 스크립트가 이미지 태그로 주입하는 값이라
  번들에 있으면 그것이 이겨 버전 표기가 틀어진다.

## 3. 최초 준비 (환경마다 한 번)

### 3.1 시크릿 만들기

1. AWS 콘솔에서 리전을 **아시아 태평양(서울) `ap-northeast-2`** 로 맞춘다.
2. **Secrets Manager → 새 보안 암호 저장** 을 누른다.
3. **보안 암호 유형**에서 **다른 유형의 보안 암호** 를 고른다.
4. **키/값 페어** 탭에서 키와 값을 하나씩 넣거나, **일반 텍스트** 탭에 아래 JSON을 붙여 넣는다.
   키 이름은 `.env`에서 쓰던 환경변수 이름과 같다(대소문자·하이픈은 가리지 않는다).

   ```json
   {
     "LLM_PROVIDER": "bedrock",
     "BEDROCK_MODEL": "...",
     "APP_SERVER_API_URL": "https://api.example.com/s/api/v1",
     "LANGFUSE_PUBLIC_KEY": "...",
     "LANGFUSE_SECRET_KEY": "...",
     "OPENAI_API_KEY": "...",
     "GEMINI_API_KEY": "..."
   }
   ```

5. **암호화 키**는 기본값(`aws/secretsmanager`)을 그대로 둔다.
6. **보안 암호 이름**을 환경별로 정한다. 예: `laimory-ai/dev/app`, `laimory-ai/prod/app`.
7. 자동 교체(로테이션)는 **비활성화** 상태로 둔다. 외부 서비스 키라 AWS가 대신 바꿀 수 없다.
8. **저장** 후 상세 화면의 **보안 암호 ARN** 을 복사해 둔다. 다음 단계에서 쓴다.

Bedrock은 API 키가 없고 IAM 역할로 인증하므로 `BEDROCK_AWS_PROFILE` 같은 값은 번들에 넣지
않는다.

### 3.2 읽기 권한 주기

권한을 받을 역할은 환경에 따라 다르다.

- **dev** — EC2 인스턴스에 붙은 역할 (예: `laimory-ai-ec2-runtime`)
- **prod** — AgentCore Runtime의 실행 역할

1. **IAM → 역할** 에서 해당 역할을 연다.
2. **권한 추가 → 인라인 정책 생성** 을 누른다.
3. **JSON** 탭에 아래를 붙여 넣는다. `Resource`에는 3.1에서 복사한 ARN을 넣되, 끝의 6자리
   임의 접미사 자리에 `*`를 쓴다.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadSecretBundle",
         "Effect": "Allow",
         "Action": "secretsmanager:GetSecretValue",
         "Resource": "arn:aws:secretsmanager:ap-northeast-2:392900063927:secret:laimory-ai/prod/app-*"
       }
     ]
   }
   ```

4. 정책 이름을 `ReadSecretBundle` 정도로 주고 **정책 생성** 을 누른다.

`Resource`를 `*`로 두지 않는다. 그 계정의 다른 시크릿까지 읽을 수 있게 된다.

### 3.3 번들 이름 알려주기

애플리케이션에 "어느 시크릿을 읽을지"만 알려주면 된다. 이 값은 비밀이 아니다.

**dev (EC2)** — 인스턴스에 접속해 env 파일에 한 줄을 추가한다.

```bash
sudo vi /opt/laimory-ai/runtime.env
# SECRETS_BUNDLE_NAME=laimory-ai/dev/app
sudo docker restart laimory-ai
```

**prod (AgentCore Runtime)** — Runtime의 환경 변수에 같은 이름을 넣는다. 콘솔에서
**Amazon Bedrock → AgentCore → 해당 Agent Runtime → 환경 변수** 를 편집하고, 콘솔에서
편집이 지원되지 않으면 [AgentCore 배포 가이드](deploy-agentcore.md) 7장의 절차를 따른다.
환경 변수를 바꾸면 새 Runtime 버전이 만들어진다.

### 3.4 옛 값 정리 (선택)

번들이 환경변수를 이기므로 급하지 않지만, 값이 두 곳에 남아 있으면 나중에 어느 쪽이 적용
중인지 헷갈린다. 번들로 옮긴 키는 `runtime.env`(또는 Runtime 환경 변수)에서 지우는 편이 낫다.

## 4. 확인

기동 로그(JSON)에서 아래를 본다. **값은 어떤 로그에도 남지 않는다.**

| 로그 | 의미 |
|---|---|
| `시크릿 번들 로드 완료` + `secretNameCount` | 번들을 읽었다. 개수가 넣은 키 수와 같은지 본다 |
| `errorCode: 1408` | 번들을 읽지 못했다. 이름·권한·리전을 확인한다 |
| 아무 로그도 없음 | `SECRETS_BUNDLE_NAME`이 비어 있다(AWS 호출 안 함) |

값이 실제로 적용됐는지는 동작으로 확인한다. 예를 들어 `APP_SERVER_API_URL`을 번들로 옮겼다면
타임라인 요청이 정상 처리되는지 본다.

## 5. 값 바꾸기

이미지를 다시 만들지 않는다.

1. **Secrets Manager → 해당 보안 암호 → 보안 암호 값 검색 → 편집**
2. 값을 고치고 **저장**
3. 컨테이너를 재시작한다 (dev: `sudo docker restart laimory-ai`, prod: Runtime 재배포)

값은 기동 시 1회만 읽으므로 **재시작 전에는 예전 값으로 동작한다.**

이전 값이 필요하면 같은 화면의 **버전** 탭에서 이전 버전(`AWSPREVIOUS`)을 확인할 수 있다.

## 6. 되돌리기

번들을 쓰기 전 상태로 돌아가려면 `SECRETS_BUNDLE_NAME`을 비우고(또는 그 줄을 지우고) 값을
`runtime.env`에 되돌린 뒤 재시작한다. 애플리케이션은 두 경로를 모두 지원하므로 이미지를
바꾸지 않아도 된다.

## 7. 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| `1408` + `AccessDeniedException` | 역할에 3.2 정책이 없거나 `Resource` ARN이 다르다 |
| `1408` + `ResourceNotFoundException` | 이름 오타이거나 시크릿이 다른 리전에 있다 |
| `1408` + `NoRegionError` | 리전을 정할 수 없다. EC2·Runtime 밖에서 실행 중이면 `AWS_REGION`을 준다 |
| `1408` + `JSONDecodeError` | 값이 JSON 객체가 아니다. 최상위가 `{ }`인지 확인한다 |
| 기동 자체가 실패 (`APP_SERVER_API_URL` 검증 오류 등) | 필수 값을 번들에만 뒀는데 조회가 실패했다. 위 1408 원인부터 본다 |
| 값을 고쳤는데 그대로다 | 컨테이너를 재시작하지 않았다 |
| 버전 표기가 배포 태그와 다르다 | `AGENT_VERSION`을 번들에 넣었다. 번들에서 지운다 |

## 관련 문서

- [EC2 컨테이너 배포 가이드](deploy-ec2.md) — 5장 Instance Role, 8장 운영 환경변수
- [AgentCore Runtime 배포 가이드](deploy-agentcore.md) — 7장 운영 환경 변수
- [오류 코드](error-codes.md) — 1408
