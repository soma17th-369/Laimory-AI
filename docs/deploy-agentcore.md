# AgentCore Runtime 배포 가이드

> 기준일: 2026-07-24
> 대상: Laimory AI 서버(FastAPI)를 Amazon Bedrock AgentCore Runtime 으로 수동 복구하는 절차

기본 자동 배포 경로는 EC2다. 이 문서는 AgentCore 장애가 해소됐을 때 Runtime 배포를
수동으로 재개하거나 기존 Runtime 버전으로 롤백하는 절차를 설명한다. EC2 운영 경로는
[EC2 컨테이너 배포 가이드](deploy-ec2.md)를 따른다.

## 1. 배포 구조

```text
Actions에서 Deploy AgentCore Runtime 수동 실행
→ GitHub Actions (deploy-agentcore.yml)
→ OIDC 로 AWS 임시 자격증명 발급
→ linux/arm64 이미지 빌드 → Amazon ECR push (태그: sha-<커밋12자>)
→ UpdateAgentRuntime  → 새 Runtime 버전 생성 (1, 2, 3 ...)
→ UpdateAgentRuntimeEndpoint → 엔드포인트를 새 버전으로 전환
```

배포의 실체는 **엔드포인트가 어느 Runtime 버전을 가리키는가**다. 롤백은 이미지를 다시
빌드하지 않고 엔드포인트를 이전 버전으로 되돌리는 것으로 끝난다.

App Server 는 이 엔드포인트를 `bedrock-agentcore:InvokeAgentRuntime` 으로 호출하고,
호출 payload 는 컨테이너의 `POST /invocations` 로 그대로 전달된다.

## 2. 컨테이너 계약

AgentCore Runtime 은 컨테이너에 아래를 고정으로 요구한다. 하나라도 어긋나면 Runtime 이
`READY` 로 올라오지 않는다.

| 요구사항 | 이 저장소의 구현 |
|---|---|
| 이미지 아키텍처 `linux/arm64` | `deploy-agentcore.yml` 의 Buildx `platforms: linux/arm64` |
| HTTP 포트 `8080` | `EXPOSE 8080` + `uvicorn --host 0.0.0.0 --port 8080` |
| `POST /invocations` | `app/api/agentcore.py` (내부적으로 `POST /v1/timeline` 과 동일 처리) |
| `GET /ping` → `{"status": "Healthy"\|"HealthyBusy"}` | `app/api/agentcore.py` |
| 이미지 위치 | 같은 계정·같은 리전의 Amazon ECR |

### `HealthyBusy` 가 필요한 이유

AI 서버는 요청을 `202` 로 접수하고 실제 처리는 백그라운드에서 이어간다. HTTP 응답이
이미 나간 뒤에도 파이프라인이 돌고 있으므로, 이때 `/ping` 이 `Healthy`(유휴)를 답하면
AgentCore 가 컨테이너를 회수해 처리가 통째로 사라질 수 있다.

`app/core/inflight.py` 가 진행 중인 처리 수를 세고, 하나라도 있으면 `/ping` 이
`HealthyBusy` 를 돌려준다. 이 값은 task 상태 저장소가 아니다 — taskId 를 담지 않고,
조회 수단이 없고, 프로세스와 함께 사라진다. task 상태는 여전히 App Server 가 소유한다.

이 때문에 **uvicorn worker 를 늘리면 안 된다.** 카운터가 프로세스 로컬이라 worker 가
여럿이면 A 가 처리 중인데 B 가 `/ping` 에 `Healthy` 를 답한다.

## 3. AWS 사전 준비

아래는 저장소 밖에서 한 번만 하는 작업이다. 예시의 `123456789012` 는 AWS 계정 번호,
리전은 `ap-northeast-2` 로 가정한다.

### 3.1 ECR 리포지토리

```bash
aws ecr create-repository \
  --repository-name laimory-ai \
  --image-scanning-configuration scanOnPush=true \
  --region ap-northeast-2
```

### 3.2 GitHub OIDC 자격증명 공급자

계정에 아직 없을 때만 만든다(계정당 하나면 된다).

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

### 3.3 배포용 IAM 역할 (GitHub Actions 가 맡는 역할)

신뢰 정책 — `dev` 브랜치에서 도는 워크플로만 이 역할을 맡을 수 있게 좁힌다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:soma17th-369/Laimory-AI:ref:refs/heads/dev"
        }
      }
    }
  ]
}
```

> 롤백 워크플로도 `dev` 브랜치에서 실행해야 이 조건을 통과한다. 다른 브랜치에서도
> 수동 실행하려면 `sub` 조건에 항목을 추가한다. 넓히는 만큼 권한도 넓어진다.

권한 정책 — ECR push 와 AgentCore 갱신, 그리고 **`iam:PassRole`** 이 필요하다.
`UpdateAgentRuntime` 이 Runtime 실행 역할 ARN 을 인자로 받기 때문에, 이게 없으면
배포가 `AccessDenied` 로 떨어진다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrLogin",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrPush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages"
      ],
      "Resource": "arn:aws:ecr:ap-northeast-2:123456789012:repository/laimory-ai"
    },
    {
      "Sid": "AgentCoreDeploy",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:UpdateAgentRuntime",
        "bedrock-agentcore:ListAgentRuntimeVersions",
        "bedrock-agentcore:GetAgentRuntimeEndpoint",
        "bedrock-agentcore:UpdateAgentRuntimeEndpoint"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassRuntimeRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/laimory-ai-agentcore-runtime",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
        }
      }
    }
  ]
}
```

> IAM 액션 접두사는 control plane(`bedrock-agentcore-control`)과 data plane
> (`bedrock-agentcore`)이 같은 `bedrock-agentcore:` 를 쓴다(두 서비스의 `signingName`
> 이 모두 `bedrock-agentcore`).

### 3.4 Runtime 실행 역할 (컨테이너가 쓰는 역할)

신뢰 정책:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

권한 정책 — ECR pull, CloudWatch Logs, Bedrock 모델 호출.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrPull",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    }
  ]
}
```

이 역할이 `BEDROCK_AWS_PROFILE` 을 비워 뒀을 때 `boto3` 기본 자격증명 체인이 집어가는
자격증명이다(`app/core/llm.py`). `.env` 나 환경 변수로 AWS 키를 넣지 않는다.

> VPC 모드(3.5)에서는 ENI 생성 권한(`ec2:CreateNetworkInterface`,
> `ec2:DescribeNetworkInterfaces`, `ec2:DeleteNetworkInterface`)이 추가로 필요할 수
> 있다. 콘솔의 AgentCore 생성 흐름으로 역할을 만들면 필요한 권한이 함께 붙으므로,
> 직접 만든 역할로 Runtime 이 `CREATE_FAILED` 가 되면 `failureReason` 을 먼저 본다.

### 3.5 네트워크 모드는 `VPC` 다

`networkMode` 는 `PUBLIC` 과 `VPC` 중 하나인데, 이 프로젝트는 **`VPC` 를 써야 한다.**
AI 서버는 입력 조회·결과 저장·완료 콜백을 전부 App Server 서버간 API 로 호출하므로
(`app/services/app_server_client.py`), App Server 에 닿는 네트워크가 필요하다.

- subnet: App Server 에 접근 가능한 private subnet
- security group: App Server security group 인바운드에 허용된 SG

staging DB 직결은 이슈 #40 에서 제거됐다. DB security group 이나 3306 경로는 더 이상
필요하지 않다.

## 4. GitHub 저장소 설정

저장소 → **Settings** → **Secrets and variables** → **Actions**

`vars` 는 Actions 로그에 그대로 찍히고 `secrets` 는 마스킹된다. 계정 번호가 박히는 role
ARN 만 secret 으로 둔다.

| 이름 | 종류 | 예시 | 출처 |
|---|---|---|---|
| `AWS_REGION` | Variable | `ap-northeast-2` | 직접 정한다 |
| `ECR_REPOSITORY` | Variable | `laimory-ai` | 리포지토리 **이름만**. `123456789012.dkr.ecr...` 는 ECR 로그인 액션이 자동으로 붙인다 |
| `AWS_DEPLOY_ROLE_ARN` | **Secret** | `arn:aws:iam::123456789012:role/laimory-ai-github-deploy` | 3.3 에서 만든 역할 ARN |
| `AGENTCORE_RUNTIME_ID` | Variable | `laimory_ai-a1B2c3D4e5` | `create-agent-runtime` 응답의 `agentRuntimeId` |
| `AGENTCORE_ENDPOINT_NAME` | Variable | `prod` | `create-agent-runtime-endpoint --name` 으로 직접 정한다 |

CLI 로 넣을 수도 있다.

```bash
gh variable set AWS_REGION --body "ap-northeast-2"
gh variable set ECR_REPOSITORY --body "laimory-ai"
gh secret   set AWS_DEPLOY_ROLE_ARN --body "arn:aws:iam::123456789012:role/laimory-ai-github-deploy"
```

### 이름 규칙 주의

Runtime 이름과 엔드포인트 이름은 `[a-zA-Z][a-zA-Z0-9_]{0,47}` 이라 **하이픈을 못 쓴다.**

- Runtime 이름: `laimory-ai` ❌ → `laimory_ai` ✅
- ECR 리포지토리 이름: 규칙이 달라서 `laimory-ai` 로 둬도 된다

Runtime 버전은 `1`, `2`, `3` 같은 숫자 문자열이다.

## 5. 최초 부트스트랩 순서

`AGENTCORE_RUNTIME_ID` 는 Runtime 을 만들어야 생기고, Runtime 은 ECR 에 이미지가 이미
있어야 만들 수 있다. 그래서 순서가 있다.

**1)** 3장의 AWS 준비를 끝낸다(ECR, OIDC, 역할 2개).

**2)** GitHub 에 먼저 3개만 등록한다 — `AWS_REGION`, `ECR_REPOSITORY`,
`AWS_DEPLOY_ROLE_ARN`.

**3)** Actions → **Deploy AgentCore Runtime** → `Run workflow` 로 수동 실행한다.

- `build` job 은 **성공**하고 이미지가 ECR 에 올라간다.
- `deploy` job 은 `AGENTCORE_RUNTIME_ID` 가 비어 있어 첫 스텝에서 **의도적으로 멈춘다.**
  `저장소 변수가 비어 있다: AGENTCORE_RUNTIME_ID ...` 메시지가 나오면 정상이다.

**4)** 올라간 이미지 태그를 확인하고(빌드 요약에 `sha-...` 로 적힌다) Runtime 을 만든다.

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name laimory_ai \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/laimory-ai:sha-0123456789ab"}}' \
  --role-arn arn:aws:iam::123456789012:role/laimory-ai-agentcore-runtime \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{"subnets":["subnet-aaaa","subnet-bbbb"],"securityGroups":["sg-cccc"]}}' \
  --protocol-configuration '{"serverProtocol":"HTTP"}' \
  --environment-variables file://runtime-env.json \
  --region ap-northeast-2
```

`runtime-env.json` 은 7장의 환경 변수 표를 참고해 만들고, **커밋하지 않는다.**

**5)** `READY` 가 되면 전용 엔드포인트를 만든다.

```bash
aws bedrock-agentcore-control create-agent-runtime-endpoint \
  --agent-runtime-id laimory_ai-a1B2c3D4e5 \
  --name prod \
  --agent-runtime-version 1 \
  --region ap-northeast-2
```

> 롤백은 엔드포인트를 특정 버전에 고정하는 방식이다. `DEFAULT` 엔드포인트는 최신 버전을
> 따라가도록 동작하므로 롤백 지점으로 쓸 수 없다. 반드시 전용 엔드포인트를 만든다.

**6)** 나머지 2개를 등록한다 — `AGENTCORE_RUNTIME_ID`, `AGENTCORE_ENDPOINT_NAME`.

**7)** 이후 필요할 때 Actions 에서 AgentCore 배포를 수동 실행한다.

## 6. 이미지 태그와 배포 이력

| 태그 | 성격 | 용도 |
|---|---|---|
| `sha-<커밋12자>` | 불변 | **배포에 쓰는 태그.** 어느 커밋이 떠 있는지의 정본 |
| `dev` | 이동 | 마지막 AgentCore 수동 배포 이미지를 가리키는 편의 태그. 배포에 쓰지 않는다 |

이동 태그로 배포하면 "지금 무엇이 떠 있는가" 가 사라져 롤백 기준이 없어진다. 그래서
`UpdateAgentRuntime` 에는 항상 `sha-` 태그를 넘긴다.

배포가 끝나면 Actions 실행 요약에 아래가 표로 남는다.

- 커밋 SHA / 이미지 태그 / 이미지 digest
- 새 Runtime 버전
- 엔드포인트 이름
- **직전 서비스 버전** (롤백할 대상)

## 7. 운영 환경 변수

`Settings`(`app/core/config.py`)가 읽는 값이다. 이미지에는 기본값 4개만 굽고, 나머지는
Runtime 의 `environmentVariables` 로 주입한다.

| 변수 | 필수 | 비고 |
|---|:---:|---|
| `APP_ENV` | O | 이미지 기본값 `prod` |
| `LOG_LEVEL` | O | 이미지 기본값 `INFO` |
| `LOG_FORMAT` | | 이미지 기본값 `json`. CloudWatch Logs Insights 용 |
| `LLM_PROVIDER` | O | 이미지 기본값 `bedrock` |
| `BEDROCK_MODEL` | O | Nova 모델 id 또는 추론 프로필 id. 비면 provider 생성 시 실패한다 |
| `BEDROCK_REGION` | | 기본 `ap-northeast-2` |
| `BEDROCK_AWS_PROFILE` | | **넣지 않는다.** 비어 있어야 실행 역할 자격증명을 쓴다 |
| `SECRETS_BUNDLE_NAME` | | 외부 시크릿 번들(#30). Secrets Manager 시크릿 이름/ARN 하나. 비우면 AWS를 호출하지 않는다. 실행 역할에 해당 ARN의 `secretsmanager:GetSecretValue` 가 필요하다 |
| `APP_SERVER_API_URL` | O | App Server 서버간 API 기본 URL(`/s/api/v1`까지). 유일한 데이터 경로라 비면 기동에 실패한다 |
| `APP_SERVER_TIMEOUT_SEC` `APP_SERVER_MAX_ATTEMPTS` `APP_SERVER_RETRY_BACKOFF_SEC` | | 기본 3초 / 3회 / 0.5초. timeout·5xx 에만 재시도한다 |
| `PIPELINE_TIMEOUT_SEC` | | 기본 120 |
| `REPAIR_MAX_ITERATIONS` | | 기본 3 |
| `LANGFUSE_ENABLED` `LANGFUSE_PUBLIC_KEY` `LANGFUSE_SECRET_KEY` `LANGFUSE_BASE_URL` | | 선택적 Langfuse tracing. 일본 리전 URL은 `https://jp.cloud.langfuse.com`. 라이프로그 본문 보호를 위해 기본 비활성 |
| `LANGFUSE_SAMPLE_RATE` `LANGFUSE_CONTENT_CAPTURE` `LANGFUSE_MAX_PAYLOAD_BYTES` | | sampling 비율, 콘텐츠 정책, observation별 payload 상한. `LANGFUSE_CONTENT_CAPTURE`는 비워 두면 `APP_ENV`로 정해진다(local/dev=`SANITIZED`, 그 외=`NONE`). 운영에서 본문을 내보내지 않으려면 비워 두거나 `NONE`을 명시한다 |

기존 Runtime에 `CALLBACK_URL`이 있으면 `APP_SERVER_API_URL`로 교체해야 한다.
값에는 task별 경로를 제외하고 `/s/api/v1`까지 넣는다. 배포 워크플로는 기존
환경변수를 그대로 보존하므로 이 이름 변경은 Runtime 설정에서 한 번 직접 반영한다.

**환경 변수는 최초 생성 때 한 번만 넣으면 된다.** 이후 배포는 `GetAgentRuntime` 으로
현재 설정을 읽어 그대로 되돌려 보내고 컨테이너 이미지 URI 만 교체한다. 덕분에 DB
접속정보 같은 운영 설정이 GitHub 에 존재하지 않는다.

값을 바꾸려면 콘솔이나 CLI 로 Runtime 을 직접 갱신한다. 그것도 새 버전을 만든다.

> `UpdateAgentRuntime` 은 부분 수정이 아니라 전체 교체다. 직접 호출할 때
> `--environment-variables` 를 빼면 기존 값이 지워진다.

## 8. 배포

AgentCore 배포는 자동 실행하지 않는다.

Actions → **Deploy AgentCore Runtime** → `Run workflow`에서 수동 실행한다. 현재
기본 자동 배포는 `deploy-ec2.yml`이 담당한다.

### 진행 순서

1. 현재 엔드포인트의 서비스 버전을 기록한다(롤백 지점).
2. `UpdateAgentRuntime` 으로 새 버전을 만든다.
3. Runtime 이 `READY` 가 될 때까지 기다린다(최대 15분).
4. `UpdateAgentRuntimeEndpoint` 로 엔드포인트를 새 버전으로 전환한다.
5. 엔드포인트가 `READY` 가 될 때까지 기다린다(최대 15분).

배포와 롤백은 같은 concurrency 그룹을 써서 동시에 돌지 않는다.

## 9. 롤백

Actions → **Rollback AgentCore Runtime** → `Run workflow`.

**버전을 비워 두고 실행하면** 현재 서비스 버전과 사용 가능한 버전 목록만 요약에
출력하고 끝난다. 무엇으로 되돌릴지 먼저 확인할 때 쓴다.

**버전을 지정해 실행하면** 엔드포인트를 그 버전으로 되돌린다. 이미지를 다시 빌드하지
않는다 — 각 Runtime 버전이 그때 배포된 ECR 이미지를 그대로 물고 있다.

지정한 버전이 존재하지 않거나 이미 서비스 중이면 전환하지 않고 실패한다.

CLI 로 직접 할 수도 있다.

```bash
aws bedrock-agentcore-control list-agent-runtime-versions \
  --agent-runtime-id laimory_ai-a1B2c3D4e5 --region ap-northeast-2

aws bedrock-agentcore-control update-agent-runtime-endpoint \
  --agent-runtime-id laimory_ai-a1B2c3D4e5 \
  --endpoint-name prod \
  --agent-runtime-version 2 \
  --region ap-northeast-2
```

## 10. 로컬 Docker 검증

`Dockerfile` 은 플랫폼을 고정하지 않는다. 아래 로컬 명령은 개발기 플랫폼으로
빌드하며, AgentCore와 같은 arm64 이미지를 확인하려면
`docker buildx build --platform linux/arm64 --load -t laimory-ai:local .`을 사용한다.
x86 개발기에서 arm64 빌드는 에뮬레이션을 사용하므로 느릴 수 있다.

```bash
docker build -t laimory-ai:local .
docker run --rm -p 8080:8080 --name laimory-ai laimory-ai:local
```

다른 터미널에서:

```bash
curl http://127.0.0.1:8080/ping
# {"status":"Healthy"}
```

`/ping` 은 DB 나 LLM 설정 없이도 응답한다(엔진과 provider 는 실제 처리 시점에 만든다).
실제 처리까지 확인하려면 `.env` 를 그대로 넘긴다. `.env` 는 `.dockerignore` 로 이미지에
들어가지 않으므로 실행 시점에만 주입된다.

```bash
docker run --rm -p 8080:8080 --env-file .env laimory-ai:local
```

아키텍처 확인:

```bash
docker image inspect laimory-ai:local --format '{{.Architecture}}'
# arm64
```

## 11. 트러블슈팅

| 증상 | 원인과 조치 |
|---|---|
| 워크플로가 `저장소 변수/시크릿이 비어 있다` 로 멈춤 | 4장의 표대로 등록한다. 부트스트랩 중이라면 5장 3) 단계에서는 정상 동작이다 |
| `Invalid choice: 'bedrock-agentcore-control'` | AWS CLI 가 오래됐다. 워크플로는 자동으로 갱신하지만, 로컬에서는 AWS CLI v2 를 최신으로 올린다 |
| Runtime 이 `CREATE_FAILED` / `UPDATE_FAILED` | `aws bedrock-agentcore-control get-agent-runtime ... --query 'failureReason'` 을 먼저 본다. 이미지 아키텍처(arm64), 포트(8080), `/ping` 응답, 실행 역할 ECR pull 권한 순으로 확인한다 |
| 배포는 되는데 DB 처리가 전부 FAILED | `networkMode` 가 `PUBLIC` 인지 확인한다. private subnet 의 DB 에 붙으려면 `VPC` 여야 한다(3.5) |
| 배포 후 `BEDROCK_MODEL` 등이 사라짐 | `UpdateAgentRuntime` 을 CLI 로 직접 부르면서 `--environment-variables` 를 뺐을 때 생긴다. 워크플로는 기존 값을 읽어 보존한다 |
| `AccessDenied` (`iam:PassRole`) | 배포 역할에 3.3 의 `PassRuntimeRole` 문이 빠졌다 |
| 백그라운드 처리가 중간에 끊김 | uvicorn worker 를 늘렸는지 확인한다. in-flight 카운터가 프로세스 로컬이라 단일 worker 여야 한다(2장) |

## 12. 알려진 한계

- **환경 변수에 넣은 값은 `GetAgentRuntime` 을 부를 수 있는 사람에게 평문으로 보인다.**
  키 계열은 `SECRETS_BUNDLE_NAME`(#30) 으로 Secrets Manager 에 두고 환경 변수에서 지운다.
  비밀이 아닌 설정(모델 id·URL·상한)은 그대로 환경 변수로 둔다.
- 배포 워크플로에 수동 승인 게이트가 없다. 필요하면 `deploy` job 에
  `environment:` 를 달고 그 환경에 reviewer 를 지정한다.
