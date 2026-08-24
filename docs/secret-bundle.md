# 시크릿 번들 운영 매뉴얼

> 기준일: 2026-08-24
> 대상: Laimory AI 서버의 외부 Provider 키를 AWS Secrets Manager로 옮기고 운영하는 절차 (이슈 #30)

애플리케이션 시크릿을 EC2의 `runtime.env`나 AgentCore `environmentVariables`에 평문으로 두지
않고, **Secrets Manager 시크릿 하나(JSON 번들)** 에서 기동 시 읽어 온다. 키를 바꿀 때 이미지를
다시 만들 필요가 없다.

## 1. 동작 방식

애플리케이션이 시크릿을 읽는 자리는 `app/core/secrets.py` 하나이고, 순서는 다음과 같다.

```text
환경변수 / .env  →  Secrets Manager 번들  →  빈 문자열
```

- `SECRETS_BUNDLE_NAME`이 **비어 있으면 AWS를 호출하지 않는다.** 로컬 개발은 지금까지와 같다.
- 번들은 **기동 시 1회** 읽어 프로세스가 사는 동안 캐시한다. 값을 바꾸면 컨테이너를 재시작한다.
- **어떤 키를 넣을지는 운영이 정한다.** 애플리케이션은 대상 목록을 갖지 않고, 번들에 있는 키를
  쓰며 없는 키는 빈 값이다. 키를 추가해도 코드는 바뀌지 않는다.
- 조회에 실패해도 **기동은 된다.** 오류코드 `1408`을 남기고 빈 값으로 진행하므로, 그 키가
  실제로 필요한 provider를 쓰고 있었다면 그 시점에 실패한다. 실패는 캐시하지 않아 일시적인
  오류는 다음 호출에서 회복된다.
- ⚠️ **환경변수가 번들보다 우선한다.** 번들로 옮긴 키는 `runtime.env`에서 **지워야** 한다.
  남겨 두면 번들 값을 고쳐도 반영되지 않는다.

## 2. 최초 준비 (한 번만)

### 2.1 시크릿 생성

리전은 애플리케이션과 같은 `ap-northeast-2`를 쓴다. 값은 **JSON 객체 하나**이고, 키 이름은
`.env`에서 쓰던 환경변수 이름과 같다(대소문자·하이픈 무관).

```json
{
  "LANGFUSE_PUBLIC_KEY": "...",
  "LANGFUSE_SECRET_KEY": "...",
  "OPENAI_API_KEY": "...",
  "GEMINI_API_KEY": "..."
}
```

콘솔은 **Secrets Manager → 새 보안 암호 저장 → 다른 유형의 보안 암호 → 일반 텍스트**에 위
JSON을 붙여 넣고 이름을 `laimory-ai/prod/app`으로 저장한다. CLI는 다음과 같다.

```bash
aws secretsmanager create-secret \
  --name laimory-ai/prod/app \
  --description "Laimory AI 애플리케이션 시크릿 번들" \
  --secret-string file://bundle.json \
  --region ap-northeast-2
```

`bundle.json`은 커밋하지 않고 작업 뒤 삭제한다.

`BEDROCK_*`, `APP_SERVER_API_URL`, `LOG_*`처럼 **비밀이 아닌 설정은 번들에 넣지 않는다.**
그 값들은 계속 환경변수로 둔다. Bedrock은 API key가 없고 IAM 역할로 인증한다.

### 2.2 실행 역할에 읽기 권한 부여

EC2 Instance Role(예: `laimory-ai-ec2-runtime`)의 인라인 정책에 다음 문을 추가한다.

```json
{
  "Sid": "ReadSecretBundle",
  "Effect": "Allow",
  "Action": "secretsmanager:GetSecretValue",
  "Resource": "arn:aws:secretsmanager:ap-northeast-2:392900063927:secret:laimory-ai/prod/app-*"
}
```

Secrets Manager는 ARN 끝에 6자리 임의 접미사를 붙이므로 `-*`로 끝낸다. 다른 시크릿까지
열리지 않도록 `Resource`를 `*`로 두지 않는다.

AgentCore Runtime으로 전환한 뒤에는 **Runtime 실행 역할**에 같은 문을 준다.

### 2.3 번들 이름 주입

EC2는 `/opt/laimory-ai/runtime.env`에 한 줄을 추가한다.

```dotenv
SECRETS_BUNDLE_NAME=laimory-ai/prod/app
```

AgentCore Runtime은 `environmentVariables`에 같은 이름을 넣는다. 자세한 절차는
[AgentCore 배포 가이드](deploy-agentcore.md) 7장을 따른다.

### 2.4 평문 제거 — 순서를 지킨다

```text
① 이 기능이 포함된 이미지 배포  →  ② 번들에 값 입력  →  ③ runtime.env 에서 해당 키 삭제
```

거꾸로 하면 그 사이에 컨테이너가 키 없이 뜬다. ③까지 끝나면 컨테이너를 재시작한다.

```bash
sudo vi /opt/laimory-ai/runtime.env    # 옮긴 키 줄을 지운다
sudo docker restart laimory-ai
```

## 3. 확인

기동 로그(JSON)에서 다음을 본다. **값은 어떤 로그에도 남지 않는다.**

| 로그 | 의미 |
|---|---|
| `시크릿 번들 로드 완료` + `secretNameCount` | 번들을 읽었다. 개수가 기대와 같은지 본다 |
| `errorCode: 1408` | 번들을 읽지 못했다. 이름·IAM 권한·리전을 확인한다 |
| `shadowedSecretNames` 경고 | 환경변수가 번들을 덮어쓰고 있다. §2.4 ③이 남았다 |
| 아무 로그도 없음 | `SECRETS_BUNDLE_NAME`이 비어 있다(AWS 호출 안 함) |

`shadowedSecretNames` 경고는 `APP_ENV`가 `prod`/`staging`일 때만 나온다. 로컬·dev에서
`.env`로 덮어쓰는 것은 정상이라 조용하다.

## 4. 키 교체

이미지를 다시 만들지 않는다.

```bash
aws secretsmanager put-secret-value \
  --secret-id laimory-ai/prod/app \
  --secret-string file://bundle.json \
  --region ap-northeast-2

sudo docker restart laimory-ai    # 값은 기동 시 1회만 읽는다
```

교체 전 값이 필요하면 `aws secretsmanager list-secret-version-ids`로 이전 버전을 확인하고
`--version-stage AWSPREVIOUS`로 되돌릴 수 있다.

## 5. 롤백

번들을 쓰기 전으로 되돌리려면 `runtime.env`에 키를 다시 넣고 `SECRETS_BUNDLE_NAME`을 비운다.
애플리케이션 코드는 두 경로를 모두 지원하므로 이미지를 바꾸지 않아도 된다.

## 6. 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| `1408` + `AccessDeniedException` | 실행 역할에 §2.2 문이 없거나 `Resource` ARN이 다르다 |
| `1408` + `ResourceNotFoundException` | 이름 오타이거나 시크릿이 다른 리전에 있다 |
| `1408` + `NoRegionError` | 리전을 정할 수 없다. EC2 밖에서 실행 중이면 `AWS_REGION`을 준다 |
| `1408` + `JSONDecodeError` | 번들 값이 JSON 객체가 아니다. 최상위가 `{ }`인지 확인한다 |
| 번들을 고쳤는데 반영되지 않음 | `runtime.env`에 같은 키가 남아 있거나 컨테이너를 재시작하지 않았다 |
| `OPENAI_API_KEY 가 설정되지 않았습니다` | 번들에도 환경변수에도 그 키가 없다. provider가 정말 그 키를 쓰는지 확인한다(Bedrock은 키가 필요 없다) |

## 관련 문서

- [EC2 컨테이너 배포 가이드](deploy-ec2.md) — 5장 Instance Role, 8장 운영 환경변수
- [AgentCore Runtime 배포 가이드](deploy-agentcore.md) — 7장 운영 환경 변수
- [오류 코드](error-codes.md) — 1408
