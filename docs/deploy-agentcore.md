# AgentCore Runtime 배포 가이드

> 기준일: 2026-08-25 (이슈 #90 배포 경계 분리 반영)
> 대상: Laimory AI 서버(FastAPI)를 AWS 웹 콘솔에서 Amazon Bedrock AgentCore Runtime으로 올리는 절차

**AgentCore Runtime이 production 운영 경로다**(이슈 #90). `main` push가
`deploy-production.yml`을 돌려 자동 배포하며, 그 승격·승인·설정 절차는
[Production 배포 가이드](deploy-production.md)에 있다. 개발 경로인 EC2 는
[EC2 컨테이너 배포 가이드](deploy-ec2.md)를 따른다.

이 문서는 그 아래 계층 — **컨테이너 계약과 AWS 자원 준비** — 를 다룬다. 어느 워크플로가
언제 도는지가 궁금하면 Production 배포 가이드를 먼저 본다.

> AgentCore 를 처음 올리는 중이라면 [AgentCore 전환 수동 작업 매뉴얼](agentcore-cutover-manual.md)
> 을 먼저 본다. 이 문서의 절차를 어떤 순서로 밟고 무엇으로 끝났는지 확인하는지를
> 체크리스트로 정리해 두었다. 이 문서는 각 콘솔 입력값과 네트워크 구성을 자세히 설명한다.

## 1. 전체 순서

```text
main push (dev → main PR merge) 또는 Actions 수동 실행
→ GitHub Actions (deploy-production.yml)
→ Environment production 승인 대기
→ OIDC 로 AWS 임시 자격증명 발급
→ linux/arm64 이미지 빌드 → Amazon ECR push (laimory-ai-prod, 불변 태그)
→ UpdateAgentRuntime  → 새 Runtime 버전 생성 (1, 2, 3 ...)
→ UpdateAgentRuntimeEndpoint → 엔드포인트를 새 버전으로 전환
```

최초 구성은 다음 순서로 진행한다.

1. AWS 콘솔에서 ECR 리포지토리를 만든다.

2. IAM 콘솔에서 GitHub OIDC 공급자, dev·production 공용 GitHub 배포 역할, Runtime 실행 역할을 준비한다.

3. VPC의 private subnet, security group, VPC endpoint와 Langfuse용 NAT Gateway를 준비한다.

4. `laimory-ai-prod`에 최초 `linux/arm64` 이미지를 올린다.

5. AgentCore 콘솔에서 ECR 이미지를 선택해 Runtime을 만든다.

6. Runtime 버전 `1`을 가리키는 전용 Endpoint `prod`를 만든다.

7. Runtime ID와 Endpoint 이름을 GitHub Environment `production`에 등록한다.

8. 실제 App Server 요청으로 호출하고 CloudWatch Logs·Langfuse·Elasticsearch 수집을 확인한다.

이후 배포는 `dev → main` PR merge로 시작하고 Environment 승인을 거친다. 문제가 생기면
`prod` Endpoint가 가리키는 Runtime 버전을 직전 버전으로 되돌린다.

### 먼저 기록할 값

| 항목 | 이 문서의 값 또는 기록할 값 |
|---|---|
| AWS 리전 | `ap-northeast-2` (서울) |
| AWS 계정 ID | `<12자리 계정 ID>` |
| production ECR 리포지토리 | `laimory-ai-prod` |
| Runtime 이름 | `laimory_ai` |
| Runtime 실행 역할 | `laimory-ai-agentcore-runtime` |
| GitHub 배포 역할 | `laimory-ai-github-deploy` |
| VPC | `<App Server에 접근 가능한 VPC>` |
| private subnet | `<서로 다른 지원 AZ의 subnet 두 개 이상>` |
| Runtime security group | `<sg-...>` |
| Secrets Manager 번들 | `laimory-ai/prod/app` 또는 실제 이름/ARN |
| Langfuse | `https://jp.cloud.langfuse.com`의 운영 프로젝트와 key pair |
| Elasticsearch | `<ES 주소>`와 `logs-laimory.ai-prod` 수집 전용 API key |
| 로그 전달 | `AgentCore CloudWatch Logs → Lambda → Elasticsearch` |
| Bedrock 모델 | `<BEDROCK_MODEL 값>` |
| App Server API | `<http://내부-DNS:8080/s/api/v1 또는 /s/v1>` |
| 전용 Endpoint | `prod` |

콘솔 작업을 시작하기 전에 우측 상단 리전이 항상 **Asia Pacific (Seoul) / ap-northeast-2**인지
확인한다. IAM은 전역 서비스지만 ECR, VPC, AgentCore, CloudWatch는 리전 선택이 중요하다.

**ECR 에 이미지가 올라가는 것만으로는 아무 일도 일어나지 않는다.** AgentCore 는 ECR 을
감시하지 않는다. 위 두 API 호출이 있어야 반영되며, 그래서 이동 태그(`latest`·`dev`)를
`containerUri` 에 쓰지 않는다 — Runtime 버전과 이미지가 1:1 이어야 롤백이 성립한다.

App Server 는 이 엔드포인트를 `bedrock-agentcore:InvokeAgentRuntime` 으로 호출하고,
호출 payload 는 컨테이너의 `POST /invocations` 로 그대로 전달된다.

진입점이 하나뿐이라 요청 종류는 payload 최상위의 `requestType`(`TIMELINE` /
`USER_MEMORY_UPDATE`)이 말한다. 형식은 [AI 서버 API 명세](ai-server-api.md#31-agentcore-호출-계약)
에 있다. `POST /v1/timeline` 과 `POST /v1/user-memory` 도 계속 열려 있어 App Server 는 HTTP
직접 호출과 AgentCore 두 경로를 모두 쓸 수 있다.

## 2. 컨테이너 계약

AgentCore Runtime은 컨테이너에 아래 계약을 요구한다. 하나라도 어긋나면 Runtime이
`READY`로 올라오지 않는다.

| 요구사항 | 이 저장소의 구현 |
|---|---|
| 이미지 아키텍처 `linux/arm64` | `deploy-production.yml` 의 Buildx `platforms: linux/arm64` |
| HTTP 포트 `8080` | `EXPOSE 8080` + `uvicorn --host 0.0.0.0 --port 8080` |
| `POST /invocations` | `app/api/agentcore.py` (`requestType` 으로 타임라인·User Memory 를 구분해 기존 핸들러에 위임) |
| `GET /ping` → `{"status": "Healthy"\|"HealthyBusy"}` | `app/api/agentcore.py` |
| 이미지 위치 | 같은 계정·같은 리전의 Amazon ECR |

AI 서버는 요청을 `202`로 접수하고 실제 처리는 백그라운드에서 이어간다. 처리 중에는
`/ping`이 `HealthyBusy`를 반환해 AgentCore가 컨테이너를 유휴 상태로 회수하지 못하게 한다.

이 때문에 **uvicorn worker를 늘리면 안 된다.** 진행 중 처리 카운터는 프로세스 로컬이라
worker가 여러 개면 한 worker가 처리 중이어도 다른 worker가 `Healthy`를 반환할 수 있다.

## 3. AWS 콘솔 사전 준비

### 3.1 ECR 리포지토리 만들기

1. [Amazon ECR 콘솔](https://console.aws.amazon.com/ecr/repositories)을 열고 리전을 서울로 맞춘다.

2. **Private repositories** → **Create repository**를 선택한다.

3. 다음 값을 입력한다.

   | 설정 | 값 |
   |---|---|
   | Repository name | `laimory-ai-prod` |
   | Image tag mutability | `Immutable` |
   | Encryption | 기본 `AES-256` |
   | Image scanning | 가능하면 scan on push 활성화 |

**production 은 `laimory-ai-prod` 를 쓴다**(이슈 #90). 개발용 `laimory-ai` 와 나뉜 이유와
lifecycle policy 를 걸지 않는 이유는
[Production 배포 가이드 §4.1](deploy-production.md)에 있다.

4. **Create repository**를 누른 뒤 Repository URI를 기록한다.

production은 이동 태그를 사용하지 않는다. 워크플로가 매번 만드는
`prod-sha-<커밋12자>-arm64-run-<실행 ID>-<시도>` 불변 태그만 Runtime에 사용하고,
Runtime 버전의 롤백 이미지를 보존하기 위해 lifecycle policy도 설정하지 않는다.

자세한 화면 설명은 [ECR private repository 생성 공식 문서](https://docs.aws.amazon.com/AmazonECR/latest/userguide/repository-create.html)를 참고한다.

### 3.2 GitHub Actions용 OIDC 공급자 만들기

계정에 `token.actions.githubusercontent.com` 공급자가 이미 있으면 이 절차는 건너뛴다.
OIDC를 쓰면 GitHub에 장기 AWS Access Key를 저장하지 않아도 된다.

1. [IAM 콘솔](https://console.aws.amazon.com/iam/) → **Identity providers** → **Add provider**로 이동한다.

2. 다음 값을 입력한다.

   | 설정 | 값 |
   |---|---|
   | Provider type | `OpenID Connect` |
   | Provider URL | `https://token.actions.githubusercontent.com` |
   | Audience | `sts.amazonaws.com` |

3. **Add provider**를 누른다.

공식 절차는 [IAM OIDC 공급자 생성 문서](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html)를 참고한다.

### 3.3 GitHub 배포 역할 만들기

> **이 역할은 dev와 production이 공용으로 사용한다**(이슈 #90). 신뢰 정책에는
> `ref:refs/heads/dev`와 `environment:production`을 모두 허용해야 한다. 전체 정책은
> [Production 배포 가이드 §4.2](deploy-production.md)를 정본으로 따른다.

먼저 IAM 콘솔 → **Policies** → **Create policy** → **JSON**에서 아래 정책을 만든다.
`123456789012`는 실제 계정 ID로 바꾼다. 정책 이름은
`laimory-ai-github-deploy-policy`로 둔다.

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

정책을 만든 뒤 역할을 만든다.

1. IAM 콘솔 → **Roles** → **Create role**로 이동한다.

2. Trusted entity type에서 **Web identity**를 선택한다.

3. Identity provider는 `token.actions.githubusercontent.com`, Audience는
   `sts.amazonaws.com`을 선택한다.

4. GitHub organization은 `soma17th-369`, repository는 `Laimory-AI`, branch는
   `dev`를 입력한다.

5. 앞에서 만든 `laimory-ai-github-deploy-policy`를 연결한다.

6. 역할 이름을 `laimory-ai-github-deploy`로 지정하고 만든다.

7. 역할의 **Trust relationships**를 열어 subject가 아래 값으로 제한됐는지 확인한다.

```text
repo:soma17th-369/Laimory-AI:ref:refs/heads/dev
```

다른 저장소나 모든 브랜치를 뜻하는 `*`로 넓히지 않는다. 공식 화면과 제한 방법은
[GitHub OIDC 역할 생성 문서](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html)를 참고한다.

### 3.4 Runtime 실행 역할 만들기

이 역할은 GitHub Actions가 아니라 실제 AgentCore 컨테이너가 사용한다.

1. IAM 콘솔 → **Roles** → **Create role** → **Custom trust policy**로 이동한다.

2. 아래 신뢰 정책을 붙여 넣는다. 계정 ID는 실제 값으로 바꾼다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeRolePolicy",
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "123456789012"
        },
        "ArnLike": {
          "aws:SourceArn": "arn:aws:bedrock-agentcore:ap-northeast-2:123456789012:*"
        }
      }
    }
  ]
}
```

3. 역할 이름을 `laimory-ai-agentcore-runtime`으로 지정해 만든다.

4. 역할의 **Permissions** → **Add permissions** → **Create inline policy** → **JSON**에서
   아래 정책을 추가한다. 계정 ID는 실제 값으로 바꾼다.

> **pull 대상은 `laimory-ai-prod` 다**(이슈 #90). production 이 저장소를 따로 쓰므로 이
> 역할이 개발 저장소만 허용하고 있으면 Runtime 생성이 다음 오류로 실패한다.
>
> ```text
> Access denied while validating ECR URI '...'. The execution role requires
> permissions for ecr:GetAuthorizationToken, ecr:BatchGetImage, and
> ecr:GetDownloadUrlForLayer operations.
> ```
>
> `ecr:GetAuthorizationToken` 은 **저장소 단위로 좁힐 수 없다.** AWS 스펙이라 반드시
> 별도 문장에서 `"Resource": "*"` 로 둔다. 나머지만 저장소 ARN 으로 좁힌다.
>
> **아래는 최소 집합이지 전체 정책이 아니다.** 콘솔이 만들어 주는 실행 역할에는 X-Ray,
> `cloudwatch:PutMetricData`, `logs:PutResourcePolicy` 등이 함께 들어 있다. 이 JSON 으로
> 통째로 덮어쓰면 그것들이 사라진다. ECR 문장만 고친다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrImageAccess",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:ap-northeast-2:123456789012:repository/laimory-ai-prod"
    },
    {
      "Sid": "RuntimeLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:PutResourcePolicy"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RuntimeTelemetry",
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RuntimeMetrics",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "bedrock-agentcore"
        }
      }
    },
    {
      "Sid": "BedrockModelInvocation",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadSecretBundle",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:laimory-ai/prod/app-*"
    }
  ]
}
```

처음에는 실제 배포를 막지 않도록 Bedrock 모델 리소스를 `*`로 두되, 사용할
`BEDROCK_MODEL`과 추론 프로필이 확정되면 해당 ARN으로 좁힌다. Runtime에 AWS Access Key를
환경 변수로 넣지 않는다. `BEDROCK_AWS_PROFILE`을 비워 두면 boto3가 이 실행 역할의 임시
자격증명을 사용한다.

`ReadSecretBundle`의 `Resource`도 Secrets Manager에서 만든 실제 번들의 ARN 접두사로
바꾼다. `*` 전체를 허용하지 않는다. 환경변수와 Secret에 넣을 값의 구분은
[런타임 설정값 구분](runtime-configuration.md)을 따른다.

AWS가 권장하는 최신 권한 항목과 콘솔 사용자 권한은
[AgentCore Runtime IAM 권한 공식 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)에서 확인한다.

### 3.5 콘솔 작업자 권한 확인

Runtime을 만드는 로그인 사용자 또는 역할에는 AgentCore 자원 생성 권한과
`laimory-ai-agentcore-runtime` 역할에 대한 `iam:PassRole`이 필요하다. VPC 모드를 처음
사용할 때는 다음 service-linked role을 자동 생성할 수 있어야 한다.

```text
AWSServiceRoleForBedrockAgentCoreNetwork
```

조직의 관리자 역할로 최초 설정한다면 별도 조치가 없을 수 있다. 제한된 운영 역할이라면
`BedrockAgentCoreFullAccess`만으로 실행 역할 생성까지 전부 되는 것은 아니므로, IAM 관리자에게
`iam:PassRole`과 `iam:CreateServiceLinkedRole` 범위를 함께 요청한다. 운영이 안정되면 AWS
관리형 전체 권한 대신 실제 Runtime ARN으로 제한한 사용자 지정 정책으로 좁힌다.

## 4. VPC 콘솔 준비

이 프로젝트는 App Server 서버간 API로 입력 조회·결과 저장·완료 콜백을 수행하므로
App Server에 접근 가능한 **VPC 모드**를 사용한다. DB에는 직접 연결하지 않는다.

### 4.1 subnet 확인

1. VPC 콘솔 → **Subnets**에서 AgentCore에 사용할 private subnet을 고른다.

2. 서로 다른 Availability Zone의 subnet을 두 개 이상 선택한다.

3. 서울 리전에서 AgentCore VPC 연결을 지원하는 AZ ID인지 확인한다.

```text
apne2-az1, apne2-az2, apne2-az3
```

계정마다 `ap-northeast-2a` 같은 AZ 이름과 실제 AZ ID의 매핑이 다를 수 있으므로
**Availability Zone ID** 열을 기준으로 확인한다.

### 4.2 Runtime security group 만들기

VPC 콘솔 → **Security groups** → **Create security group**에서
`laimory-ai-agentcore-runtime` security group을 만든다.

현재 구성에서는 같은 VPC의 서로 다른 subnet에 있는 App Server 두 대가 공통 security
group을 사용하고, Runtime이 `HTTP 8080`으로 직접 호출한다. 다음 두 규칙을 설정한다.

**Runtime security group outbound**

| 유형 | 프로토콜 | 포트 | 대상 |
|---|---|---:|---|
| Custom TCP | TCP | `8080` | App Server 두 대가 함께 사용하는 security group |
| HTTPS | TCP | `443` | `0.0.0.0/0` — Langfuse NAT 경로와 AWS Interface Endpoint |

**App Server 공통 security group inbound**

| 유형 | 프로토콜 | 포트 | 소스 |
|---|---|---:|---|
| Custom TCP | TCP | `8080` | `laimory-ai-agentcore-runtime` security group |

`APP_SERVER_API_URL`도 `http://<사설 주소 또는 내부 DNS>:8080/s/api/v1` 또는
`http://<사설 주소 또는 내부 DNS>:8080/s/v1` 형태로 맞춘다. 두 App Server가 같은
security group을 사용하므로 서버별 규칙은 만들지 않는다. AgentCore에 선택하는 private
subnet이 두 개여도 두 subnet에 같은 Runtime security group을 지정하므로 규칙은 한 번만
설정한다.

Runtime은 App Server의 사설 IP나 `8080` 포트로부터 직접 호출을 받지 않는다. App Server는
AWS의 `InvokeAgentRuntime` API를 호출하고 AgentCore 관리 영역이 컨테이너의
`POST /invocations`로 전달한다. 따라서 Runtime security group의 inbound 규칙은 필요하지
않다.

새 security group에는 기본으로 `All traffic → 0.0.0.0/0` outbound가 들어갈 수 있다.
이 규칙을 그대로 두면 App Server 접근은 이미 허용되지만, 최소 권한으로 제한할 때는
삭제하고 위 표의 `TCP 8080`과 `HTTPS 443` 두 규칙으로 교체한다. ECR, CloudWatch,
Bedrock 같은 AWS 서비스는 다음 절의 VPC endpoint를 사용하고 Langfuse는 NAT를 사용하지만,
Runtime security group에서는 둘 다 outbound HTTPS `443`으로 보인다.

특히 `LANGFUSE_ENABLED=true`로 운영하려면 Runtime security group의 outbound `TCP 443`이
반드시 허용되어야 한다. NAT Gateway 자체에는 security group을 연결하지 않으므로
`Runtime security group → NAT security group` 규칙은 만들 수 없다. 목적지를
`0.0.0.0/0`으로 두고 private subnet route table의 `0.0.0.0/0 → NAT Gateway`가 실제
인터넷 경로를 결정한다.

CIDR 전체를 열기보다 security group 간 참조를 우선한다.

### 4.3 private subnet 통신 경로 준비

현재 구성에서는 App Server EC2 두 대와 AgentCore Runtime이 모두 private subnet에 있다.
AgentCore 호출과 AWS 서비스 접근은 VPC endpoint를 사용하고, Langfuse Cloud처럼 VPC 밖의
공개 서비스로 나갈 때만 NAT Gateway를 사용한다.

```text
App Server EC2 → AgentCore data plane VPC endpoint → AgentCore Runtime
AgentCore Runtime → Runtime VPC ENI → App Server HTTP 8080
AgentCore Runtime → NAT Gateway → Langfuse Cloud HTTPS 443
AgentCore Runtime stdout → CloudWatch Logs → 전달 Lambda → Elasticsearch
```

두 번째 경로는 4.2장의 security group 규칙이 담당한다. 첫 번째 경로를 위해 AgentCore
data plane Interface VPC Endpoint를 별도로 만든다. 마지막 경로는 Runtime이
Elasticsearch를 직접 호출하는 경로가 아니며 4.3.3장에서 따로 구성한다.

#### 4.3.1 App Server가 AgentCore를 호출하는 Endpoint

VPC 콘솔 → **Endpoints** → **Create endpoint**에서 다음 값으로 생성한다.

| 설정 | 값 |
|---|---|
| Service category | AWS services |
| Service name | `com.amazonaws.ap-northeast-2.bedrock-agentcore` |
| Endpoint type | Interface |
| VPC | App Server와 AgentCore가 사용하는 VPC |
| Subnets | App Server EC2 두 대가 있는 private subnet 두 개 |
| Private DNS | 활성화 |
| Security group | 새 `laimory-ai-agentcore-vpce` security group |

두 subnet이 서로 다른 AZ라면 각 AZ에 endpoint ENI가 하나씩 생긴다. VPC의 **DNS
resolution**과 **DNS hostnames**도 모두 활성화한다. 그러면 App Server는 별도의 endpoint
URL을 코드에 넣지 않아도 기본 주소 `bedrock-agentcore.ap-northeast-2.amazonaws.com`을
사설 endpoint IP로 해석한다.

`laimory-ai-agentcore-vpce` security group에는 다음 규칙을 둔다.

**AgentCore VPC Endpoint security group inbound**

| 유형 | 프로토콜 | 포트 | 소스 |
|---|---|---:|---|
| HTTPS | TCP | `443` | App Server EC2 두 대가 함께 사용하는 security group |

**App Server 공통 security group outbound**

| 유형 | 프로토콜 | 포트 | 대상 |
|---|---|---:|---|
| HTTPS | TCP | `443` | `laimory-ai-agentcore-vpce` security group |

App Server security group에 기본 `All traffic → 0.0.0.0/0` outbound가 남아 있다면 이미
허용된 상태다. 최소 권한으로 제한할 때 위 규칙으로 교체한다.

Endpoint policy는 최초 연결 확인 때 **Full access**로 시작할 수 있다. 확인 후에는 App
Server EC2 공통 역할과 해당 Runtime·`prod` Endpoint만 허용하도록 아래처럼 좁힌다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<계정 ID>:role/<App Server EC2 공통 역할>"
      },
      "Action": "bedrock-agentcore:InvokeAgentRuntime",
      "Resource": [
        "arn:aws:bedrock-agentcore:ap-northeast-2:<계정 ID>:runtime/<Runtime ID>",
        "arn:aws:bedrock-agentcore:ap-northeast-2:<계정 ID>:runtime/<Runtime ID>/runtime-endpoint/prod"
      ]
    }
  ]
}
```

EC2는 Runtime 호출만 하므로 control plane endpoint인
`com.amazonaws.ap-northeast-2.bedrock-agentcore-control`은 만들지 않는다. Runtime 생성과
업데이트는 GitHub Actions와 AWS 콘솔에서 수행한다.

#### 4.3.2 Runtime이 AWS 서비스에 접근하는 Endpoint

AWS 서비스 트래픽을 NAT 대신 AWS 네트워크 안으로 보내려면 VPC 콘솔 → **Endpoints** →
**Create endpoint**에서 다음도 준비한다.

| 종류 | 서비스 이름 | 설정 |
|---|---|---|
| Interface | `com.amazonaws.ap-northeast-2.ecr.dkr` | Runtime private subnet, private DNS 활성화 |
| Interface | `com.amazonaws.ap-northeast-2.ecr.api` | Runtime private subnet, private DNS 활성화 |
| Gateway | `com.amazonaws.ap-northeast-2.s3` | Runtime subnet의 route table 연결 |
| Interface | `com.amazonaws.ap-northeast-2.logs` | Runtime private subnet, private DNS 활성화 |
| Interface | `com.amazonaws.ap-northeast-2.bedrock-runtime` | Runtime private subnet, private DNS 활성화 |
| Interface | `com.amazonaws.ap-northeast-2.secretsmanager` | Runtime private subnet, private DNS 활성화 |

이 Interface endpoint들의 security group은 Runtime security group에서 들어오는 TCP
`443`을 허용한다. ECR 이미지 layer는 S3에 저장되므로 ECR endpoint만 만들고 S3 Gateway
endpoint를 빼면 이미지 pull이나 주기적 image refresh가 실패할 수 있다.

S3 Gateway Endpoint에는 시간당 요금이 없지만 Interface Endpoint는 선택한 AZ마다 시간당
요금과 데이터 처리 요금이 발생한다. 두 subnet을 선택하면 각 Interface Endpoint에 endpoint
ENI가 두 개 생긴다.

#### 4.3.3 Langfuse와 Elasticsearch 전송 경로

Langfuse와 Elasticsearch는 같은 관측 기능이지만 전송 주체와 네트워크 경로가 다르다.

| 대상 | 전송 주체 | 경로 | Runtime에 필요한 설정 |
|---|---|---|---|
| Langfuse | 애플리케이션의 Langfuse SDK | Runtime → NAT Gateway → Langfuse Cloud `443` | 일반 설정은 `LANGFUSE_*` 환경 변수, secret key는 Secrets Manager |
| Elasticsearch | 별도 로그 전달 Lambda | Runtime stdout → CloudWatch Logs → Lambda → Elasticsearch | `LOG_FORMAT=json`만 필요 |

##### Langfuse Cloud용 NAT Gateway

`LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com`은 공개 인터넷 주소다. Interface VPC
Endpoint가 없으므로 Runtime의 private subnet에 NAT 경로를 만든다.

1. VPC 콘솔 → **Internet gateways**에서 이 VPC에 연결된 Internet Gateway가 있는지
   확인하고, 없으면 생성해 연결한다.

2. 기존 public subnet이 있으면 사용한다. 새로 만들 때는 VPC CIDR 안에서 기존 subnet과
   겹치지 않는 CIDR을 사용하고, 그 subnet의 route table에
   `0.0.0.0/0 → Internet Gateway`를 둔다.

3. VPC 콘솔 → **NAT gateways** → **Create NAT gateway**에서 public subnet과 새 Elastic
   IP를 선택해 `laimory-ai-agentcore-nat`를 만든다.

4. Runtime에 지정할 private subnet 두 개의 route table에
   `0.0.0.0/0 → laimory-ai-agentcore-nat`를 추가한다. 최초 배포는 NAT 하나를 공유할 수
   있지만, 한 AZ 장애에도 외부 관측을 유지해야 하면 AZ마다 NAT Gateway를 두고 각 private
   subnet이 같은 AZ의 NAT를 사용하게 한다.

5. Runtime security group outbound에 `HTTPS / TCP 443 → 0.0.0.0/0`을 허용한다. Security
   group은 NAT Gateway를 대상으로 참조할 수 없으므로 목적지를 IPv4 전체로 두고, 실제
   경로는 private subnet의 route table이 NAT로 제한한다.

NAT는 Runtime이 시작한 연결의 응답만 되돌려 주므로 이 설정 때문에 Runtime inbound를
열 필요는 없다. public photo URL 다운로드나 OpenAI·Gemini provider를 사용해도 같은 NAT
경로를 쓴다. NAT Gateway에는 시간당 요금과 데이터 처리 요금이 발생한다.

##### CloudWatch Logs에서 Elasticsearch로 전달

AgentCore Runtime에는 EC2의 `laimory-filebeat` sidecar가 없다. 또한 이 애플리케이션은
Elasticsearch를 직접 호출하지 않도록 설계되어 있으므로 Runtime 환경 변수에 `ES_URL`,
`ES_API_KEY`, `OBS_*`를 추가하거나 Runtime security group에 ES 포트를 열지 않는다.

전달 함수 `laimory-agentcore-logs-to-es`는 **이미 운영 중이며 이 저장소에는 없다.**
소스는 AWS 콘솔에 있고 앱 PR 리뷰를 받지 않으므로, 아래는 구축 절차이자 **현재 동작 계약**이다.
고칠 때 이 절과 어긋나면 이 문서를 함께 갱신한다.

> **수집 경계는 레벨이 아니라 표식이다.** 구독 필터도 Lambda도 `event.dataset` 만 보고
> `log.level` 은 보지 않는다. WARNING 운영 이벤트(`dependency.request.retry`,
> `app.degraded`)와 ERROR 운영 이벤트는 지금도 그대로 전달된다. 앱이 새 운영 이벤트를
> 추가해도 **구독 필터와 Lambda를 바꿀 필요가 없다.**
>
> 반대로 `report_error()` 와 `logger.warning()` 이 남기는 **진단 줄**은 표식이 없어
> 계속 전달되지 않는다. 그 줄은 CloudWatch Logs Insights에서 본다.

> **오류 상세는 이제 표식 달린 줄에도 실린다(#109 범위 확장).** 실패 이벤트
> (`app.degraded`·`http.request.completed`)가 `errorMessage`·`errorStackTrace` 를 싣는다.
> prod 는 AgentCore 가 컨테이너를 회수해 `docker logs` 라는 선택지가 없어, 이것이 없으면
> Kibana 에서 실패를 보고도 원인을 볼 방법이 없기 때문이다.
>
> **전달 Lambda 의 `_SENSITIVE_KEYS` 에서 `"errormessage"` 를 빼야 한다.** 그대로 두면
> dev(EC2)에는 값이 들어오고 prod 에는 안 들어오는, 가장 알아채기 어려운 형태로 갈린다.
> `_SENSITIVE_PATHS` 의 `("error","message")`·`("error","stack_trace")` 는 **그대로 둔다** —
> 그쪽은 표식 없는 일반 로그의 예외 필드이고, 두 목록이 분리돼 있어 한쪽만 열 수 있다.
> 이름이 camelCase 와 점 표기로 갈린 이유가 그것이다.
>
> **`errorStackTrace` 는 지금도 `_SENSITIVE_KEYS` 에 없어 이미 통과한다.** 즉 Lambda 를
> 고치지 않아도 prod 는 traceback 을 받고 메시지만 잃는다. 그 상태는 안전해 보이지만
> 아니다 — `traceback.format_exception` 의 마지막 줄이 `RuntimeError: <메시지>` 라서
> **원문이 스택 안에 그대로 실려 온다.** `"errormessage"` 를 빼는 것은 방어를 푸는 것이
> 아니라 이미 열려 있는 것을 목록에 정직하게 맞추는 일이다. 반대로 정말 막고 싶다면
> `"errorstacktrace"` 를 **더해야** 하며, 그때는 두 필드를 함께 막아야 뜻이 선다.

1. Lambda 콘솔에서 같은 리전의 전달 함수 `laimory-agentcore-logs-to-es`를 만든다. 함수는
   CloudWatch Logs의 base64+gzip payload를 풀고 각 `logEvents[].message`를 JSON으로
   해석해야 한다.

2. 함수 안에서 애플리케이션이 출력한 dotted key를 중첩 객체로 펼친 뒤
   `event.dataset == "laimory.api"`인 이벤트만 통과시킨다. 표식이 없거나 JSON 해석에
   실패하면 버리는 fail-closed 방식이어야 한다. `timestamp`는 `@timestamp`로 옮긴다.

3. [`observability/filebeat.example.yml`](observability/filebeat.example.yml)의 processor
   순서와 `drop_fields`를 그대로 기준으로 삼아 민감 필드 제거를 적용하고, 목적지를
   `logs-laimory.ai-prod` data stream으로 고정한다. 전체 필드 계약은
   [운영 로그 문서](operational-logging.md)를 따른다.

   그 목록이 **정본**이다. 앱이 새로 여는 필드(`errorMessage`·`errorStackTrace`)는
   템플릿에서 빠지므로, 함수의 목록을 템플릿과 대조하지 않으면 dev 와 prod 가 서로
   다른 것을 적재한다. 함수를 고칠 때마다 이 대조를 한다.

4. Elasticsearch API key는 Lambda 환경 변수에 평문으로 넣지 않고 Secrets Manager에
   저장한다. Lambda 실행 역할에는 해당 secret을 읽는 권한과 실패 로그를 CloudWatch에
   쓰는 권한만 부여한다. API key의 Elasticsearch 권한은 `logs-laimory.ai-*`에 대한
   `auto_configure`와 `create_doc`로 제한한다.

5. CloudWatch 콘솔 → **Log groups**에서 `/aws/bedrock-agentcore/runtimes/`로 시작하는 실제
   Runtime log group을 선택하고 **Actions → Subscription filters → Create Lambda
   subscription filter**로 이동한다. 대상 함수는 위 Lambda, filter pattern은 다음과 같다.

   ```text
   { $.['event.dataset'] = "laimory.api" }
   ```

   수집 원본은 이 log group의 `[runtime-logs]` 표준 stdout/stderr 스트림에 있는 애플리케이션
   JSON이다. AgentCore의 별도 `APPLICATION_LOGS` delivery는 요청·응답 payload를 포함할 수
   있으므로 Elasticsearch 원본으로 사용하지 않는다. 같은 log group에 다른 스트림이 있어도
   위 표식이 없는 이벤트는 subscription filter에서 제외된다.

6. 콘솔의 **Test pattern**으로 운영 이벤트만 선택되는지 확인한 뒤 구독을 시작한다.
   Lambda에서도 같은 표식을 다시 검사하므로 일반 진단 로그나 사용자 본문이 실수로 ES에
   들어가지 않는다. 콘솔이 함수의 resource-based policy에 `logs.amazonaws.com`의 호출
   권한을 추가했는지 Lambda의 **Configuration → Permissions**에서도 확인한다. 전달 Lambda
   자신의 log group에는 이 구독을 걸지 않아 재귀 전송을 막는다.

7. Lambda가 Elasticsearch bulk 요청에 실패하면 invocation 자체를 실패시켜 CloudWatch
   Logs가 재시도하게 하고, `logEvents[].id`를 ES 문서 ID로 사용해 재시도 중복을 막는다.
   같은 ID의 `create` 재시도에서 받은 `409`는 이미 저장된 이벤트이므로 성공으로 처리한다.
   CloudWatch의 `AWS/Logs` namespace에서 `DeliveryErrors`와 `DeliveryThrottling`에 경보를
   만든다. 재시도 가능한 전달 오류도 24시간 뒤에는 유실될 수 있으므로 경보를 운영 필수로 둔다.

Elasticsearch가 같은 VPC의 사설 주소라면 Lambda를 ES에 도달 가능한 private subnet에
연결하고 `laimory-agentcore-log-forwarder` security group을 부여한다.

**전달 Lambda security group outbound**

| 유형 | 프로토콜 | 포트 | 대상 |
|---|---|---:|---|
| Custom TCP | TCP | `9200` | Elasticsearch security group |
| HTTPS | TCP | `443` | Secrets Manager Interface Endpoint security group |

**Elasticsearch security group inbound**

| 유형 | 프로토콜 | 포트 | 소스 |
|---|---|---:|---|
| Custom TCP | TCP | `9200` | `laimory-agentcore-log-forwarder` security group |

실제 Elasticsearch가 HTTPS `443`을 사용한다면 첫 표의 `9200` outbound와 두 번째 표의
`9200` inbound를 모두 `443`으로 맞춘다. Secrets Manager Interface Endpoint를 만들지
않고 NAT로 접근한다면 Lambda outbound도 `HTTPS 443 → 0.0.0.0/0`으로 허용하고 Lambda
private subnet의 route table이 NAT Gateway를 가리키게 한다. 전달 Lambda는 연결을
시작하는 쪽이므로 inbound 규칙은 필요하지 않다.

Elasticsearch가 Elastic Cloud 같은 공개 주소라면 Lambda를 VPC에 연결하지 않을 때 기본
인터넷 경로를 사용할 수 있다. 고정 출발지 IP가 필요해 Lambda를 VPC에 연결한다면 private
subnet과 NAT Gateway를 함께 사용하고 Lambda security group에
`HTTPS 443 → 0.0.0.0/0` outbound를 허용한다. 어느 경우든 Elasticsearch 자격증명과 연결은
전달 Lambda의 책임이며 AgentCore Runtime의 책임이 아니다.

AgentCore 호출용 PrivateLink는 [AgentCore Interface VPC Endpoint 공식 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html),
Runtime VPC 요구사항은 [AgentCore VPC 연결 공식 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html),
Bedrock endpoint 설정은 [Bedrock PrivateLink 공식 문서](https://docs.aws.amazon.com/bedrock/latest/userguide/vpc-interface-endpoints.html),
NAT route 구성은 [VPC NAT 공식 문서](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat.html),
AgentCore stdout log group 형식은 [AgentCore 관측 데이터 조회 공식 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-view.html),
CloudWatch Logs 구독 대상과 형식은 [CloudWatch Logs subscription filter 공식 문서](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/SubscriptionFilters.html)를 참고한다.

## 5. 최초 production 이미지 올리기

production 워크플로는 Runtime ID와 Endpoint 이름을 빌드 전에 검사한다. 두 값은 Runtime을
만들어야 생기므로 **최초 한 번은 워크플로 밖에서** `linux/arm64` 이미지를 올린다.

1. [Production 배포 가이드 §5](deploy-production.md)의 bootstrap 명령으로
   `laimory-ai-prod`에 이미지를 push한다.

2. 태그는 `prod-sha-<커밋12자>-arm64-bootstrap`처럼 불변 값으로 지정한다.

3. ECR 콘솔 → `laimory-ai-prod` → **Images**에서 아키텍처가 `arm64`인 이미지 URI를 복사한다.

`-amd64-run-...` 태그는 EC2 개발 배포용 이미지이므로 선택하지 않는다. bootstrap 이후에는
`dev → main` PR merge와 **Deploy Production** 워크플로가 production 이미지를 만든다.

## 6. AgentCore 콘솔에서 최초 Runtime 만들기

AgentCore 콘솔 화면 명칭은 서비스 업데이트에 따라 **Host agent** 또는
**Create runtime**으로 보일 수 있다. 핵심은 배포 소스로 ECR container image를 선택하는 것이다.

1. [Amazon Bedrock AgentCore 콘솔](https://console.aws.amazon.com/bedrock-agentcore/home#)을 열고
   리전을 서울로 맞춘다.

2. **Agents** 또는 **Runtime** 목록에서 **Host agent / Create runtime**을 선택한다.

3. 배포 소스로 **Container image / Amazon ECR**을 선택한다.

4. 다음 값을 입력한다.

   | 설정 | 값 |
   |---|---|
   | Runtime name | `laimory_ai` |
   | Container image URI | ECR에서 복사한 `.../laimory-ai-prod:prod-sha-<커밋12자>-arm64-bootstrap` |
   | Protocol | `HTTP` |
   | Execution role | 기존 역할 `laimory-ai-agentcore-runtime` |
   | Network mode | `VPC` |
   | VPC | 4장에서 확인한 VPC |
   | Subnets | 서로 다른 지원 AZ의 private subnet 두 개 이상 |
   | Security groups | `laimory-ai-agentcore-runtime` |

5. 별도의 JWT authorizer를 요구하지 않는다면 기본 AWS IAM 인증을 유지한다. App Server는
   `bedrock-agentcore:InvokeAgentRuntime` 권한으로 호출한다.

6. Lifecycle 설정은 최초 배포에서는 기본값을 유지한다. 애플리케이션의 처리 상한은
   `PIPELINE_TIMEOUT_SEC`와 `USER_MEMORY_TIMEOUT_SEC`가 관리하며, 처리 중에는 `/ping`이
   `HealthyBusy`를 반환한다.

7. [런타임 설정값 구분](runtime-configuration.md)의 1장에 있는 production 값을 Runtime
   환경 변수에 직접 입력한다. AgentCore가 값을 프로세스 환경으로 주입하므로 로컬 `.env`와
   같은 키 이름을 사용한다. boto3로 AgentCore 환경변수를 다시 조회하지 않는다.

   Secrets Manager의 `laimory-ai/prod/app`에는 2장에 적힌 실제 비밀값만 넣는다. 현재
   Bedrock 구성에서는 `LANGFUSE_SECRET_KEY` 하나이며, Runtime 환경 변수의
   `SECRETS_BUNDLE_NAME=laimory-ai/prod/app`이 이 Secret을 가리킨다.

   `BEDROCK_AWS_PROFILE`은 만들지 않는다. `CALLBACK_URL`이라는 예전 변수가 남아 있다면
   제거하고 `APP_SERVER_API_URL`을 사용한다. `ES_URL`, `ES_API_KEY`, `ES_EVENT_INDEX`,
   `OBS_*`도 만들지 않는다. Elasticsearch 전송 설정은 4.3.3장의 전달 Lambda에만 둔다.

8. 설정 요약에서 image URI, 역할, VPC, subnet, security group, 환경 변수를 다시 확인한 뒤
   생성한다.

9. Runtime 세부 화면에서 최초 버전의 상태가 `READY`가 될 때까지 기다린다. 실패하면
   같은 화면의 failure reason을 먼저 확인한다.

AWS 공식 콘솔 VPC 설정 순서는 [AgentCore Runtime VPC 구성 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)에 나와 있다.

### 이름 규칙

Runtime 이름과 Endpoint 이름에는 하이픈 대신 밑줄을 사용한다.

- Runtime 이름: `laimory-ai` 대신 `laimory_ai`

- ECR 리포지토리 이름: `laimory-ai`

- 전용 Endpoint 이름: `prod`

## 7. 전용 Endpoint 만들기

Runtime 생성 시 `DEFAULT` Endpoint가 자동으로 보일 수 있다. `DEFAULT`는 최신 버전을
따라갈 수 있으므로 명시적인 롤백 지점으로 사용하지 않는다.

1. Runtime 세부 화면의 **Endpoints** 표에서 **Create endpoint**를 선택한다.

2. 이름을 `prod`로 지정한다.

3. Associated version 또는 Runtime version으로 최초 `READY` 버전인 `1`을 선택한다.

4. Endpoint 상태가 `READY`가 될 때까지 기다린다.

5. Runtime 세부 화면에서 다음 값을 기록한다.

   - Runtime ID: `laimory_ai-...`

   - Runtime ARN

   - Endpoint name 또는 qualifier: `prod`

6. GitHub 저장소 → **Settings** → **Environments** → **production** →
   **Environment variables**에 아래 값을 추가한다.

   | 이름 | 값 |
   |---|---|
   | `AGENTCORE_RUNTIME_ID` | 콘솔에서 복사한 Runtime ID |
   | `AGENTCORE_ENDPOINT_NAME` | `prod` |

Endpoint 생성과 버전 변경 화면은 [AgentCore 콘솔 배포 문서](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)의
**Create endpoint**, **Update endpoint** 절차를 참고한다.

## 8. 최초 배포 검증

### 8.1 상태와 로그 확인

1. AgentCore Runtime 상태와 `prod` Endpoint 상태가 모두 `READY`인지 확인한다.

2. Runtime의 Observability 또는 Logs 링크를 열거나, CloudWatch 콘솔 → **Log groups**에서
   `/aws/bedrock-agentcore/runtimes/`로 시작하는 log group을 찾는다.

3. `[runtime-logs]`로 시작하는 표준 stdout/stderr log stream을 열고 기동 로그에 설정 검증
   오류, ECR pull 오류, App Server 연결 오류가 없는지 확인한다.

### 8.2 실제 호출 확인

Endpoint의 **Test endpoint**를 누르면 Playground/Sandbox에서 payload를 보낼 수 있다.
이 서버의 `/invocations`는 일반 채팅 prompt가 아니라 아래 계약을 받는다.

```json
{
  "requestType": "TIMELINE",
  "payload": {
    "taskId": "<App Server가 만든 실제 task ID>",
    "taskToken": "<App Server가 발급한 실제 task token>",
    "dailyRecordId": 123,
    "window": {
      "startAt": "2026-08-25T00:00:00+09:00",
      "endAt": "2026-08-26T00:00:00+09:00"
    }
  }
}
```

`requestType`은 `TIMELINE` 또는 `USER_MEMORY_UPDATE`이고, `payload`에는 각각 대응하는
`POST /v1/timeline` 또는 `POST /v1/user-memory` 요청 본문을 그대로 넣는다. `requestType`이
아예 없는 타임라인 본문도 두 번째 정식 형식으로 계속 지원하지만, 신규 연동과 콘솔 검증은
위 envelope 형식을 사용한다. 전체 계약은
[AI 서버 API 명세](ai-server-api.md#31-agentcore-호출-계약)를 따른다.

가짜 task나 token으로 운영 Runtime을 호출하면 접수 후 백그라운드 처리가 실패하므로,
실제 App Server가 만든 테스트 작업이 있을 때만 호출한다. `taskToken`은 문서, 스크린샷,
CloudWatch 검색어에 남기지 않는다.

정상 접수 응답은 HTTP `202`이며 body는 다음 형태다.

```json
{
  "taskId": "<같은 task ID>",
  "status": "PROCESSING"
}
```

최종 성공 여부는 AI 서버가 저장하지 않는다. App Server의 결과 저장과 완료 콜백, 그리고
CloudWatch 로그를 함께 확인한다.

### 8.3 App Server에 전달할 값

App Server 담당 설정에는 아래 세 값을 전달한다.

- Runtime ARN

- Runtime Endpoint ARN

- Endpoint qualifier `prod`

App Server EC2 두 대가 사용하는 공통 Instance Role에 Runtime과 `prod` Endpoint 모두에
대한 호출 권한을 추가한다. 두 EC2가 같은 역할을 사용하면 정책은 한 번만 추가하면 된다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeAgentRuntime",
      "Resource": [
        "arn:aws:bedrock-agentcore:ap-northeast-2:<계정 ID>:runtime/<Runtime ID>",
        "arn:aws:bedrock-agentcore:ap-northeast-2:<계정 ID>:runtime/<Runtime ID>/runtime-endpoint/prod"
      ]
    }
  ]
}
```

App Server는 AWS SDK의 AgentCore data plane client에 Runtime ARN과 qualifier `prod`를
넘겨 호출한다. 4.3.1장에서 Private DNS를 활성화했으므로 코드에 VPC Endpoint 전용 URL을
넣지 않는다.

반대 방향의 `APP_SERVER_API_URL`에는 App Server EC2 한 대의 사설 IP를 직접 넣지 않는다.
두 대를 함께 가리키는 내부 ALB DNS 또는 private DNS를 사용하고, HTTP `8080` 경로를
설정한다. 호출 payload의 `taskToken`은 요청 body로만 전달하고 운영 로그에 남기지 않는다.

### 8.4 Langfuse와 Elasticsearch 수집 확인

8.2장의 실제 작업 한 건을 완료한 뒤 같은 `taskId`로 세 경로를 차례로 확인한다.

1. CloudWatch Logs Insights에서 Runtime log group을 선택하고 아래 쿼리로
   `event.dataset=laimory.api` 운영 이벤트가 있는지 확인한다.

   ```text
   fields @timestamp, @message
   | filter @message like /"event\.dataset"\s*:\s*"laimory\.api"/
   | sort @timestamp desc
   | limit 20
   ```

2. Langfuse 운영 프로젝트에서 `session:=<taskId>`로 검색해 `generate-timeline` trace와
   하위 agent·generation이 보이는지 확인한다. 운영 설정이 `NONE`이면 trace 구조, model,
   token usage는 보이되 prompt와 응답 본문은 마스킹되는 것이 정상이다.

3. Lambda 콘솔 → `laimory-agentcore-logs-to-es` → **Monitor**에서 invocation error가
   없는지 확인하고, CloudWatch의 `AWS/Logs` `DeliveryErrors`·`DeliveryThrottling`도 0인지
   확인한다.

4. Kibana Discover에서 `logs-laimory.ai-prod`를 열어 같은 `taskId`의 운영 이벤트가
   도착했는지 확인한다. 모든 문서에 `event.dataset=laimory.api`와 `event.action`이 있어야
   하며 `taskToken`, 인증 헤더, prompt, response, 오류 원문은 없어야 한다.

CloudWatch에는 있는데 Elasticsearch에 없다면 Runtime 환경 변수나 Runtime security
group을 바꾸지 않는다. CloudWatch subscription filter, 전달 Lambda 로그, Secrets Manager
권한, Elasticsearch 인증과 Lambda→ES 네트워크 순으로 확인한다.

## 9. 이후 배포

### 9.1 GitHub 웹 화면에서 배포하기

정상적인 반복 배포는 `dev`에서 `main`으로 승격하는 PR을 사용한다.

1. 배포할 변경이 `dev`에 반영됐는지 확인한다.

2. `dev → main` PR을 열고 `dev 브랜치에서 온 PR 인지 확인` check가 통과했는지 확인한다.

3. PR을 merge하면 **Deploy Production** 워크플로가 시작되고 Environment 승인을 기다린다.

4. 승인 후 워크플로가 새 `linux/arm64` 이미지를 `laimory-ai-prod`에 올리고 새 Runtime 버전을 만든다.

5. 새 Runtime이 `READY`가 되면 워크플로가 `prod` Endpoint를 새 버전으로 전환한다.

6. 실행 요약의 이미지 태그, digest, 새 Runtime 버전, 직전 서비스 버전을 기록한다.

7. AgentCore 콘솔에서 `prod`의 live version과 상태를 확인한다.

`dev` push는 EC2 개발 배포만 실행한다. production 배포의 정본은 `main`이며 상세 승인·복구
절차는 [Production 배포 가이드](deploy-production.md)를 따른다.

### 9.2 콘솔에서 직접 새 버전으로 바꾸기

ECR에 새 `prod-sha-...-arm64-...` 이미지가 이미 있는 경우에는 콘솔에서도 배포할 수 있다.

1. AgentCore 콘솔 → `laimory_ai` Runtime 세부 화면으로 이동한다.

2. 현재 `prod` Endpoint의 live version을 메모한다.

3. **Update hosting**을 선택한다.

4. 새 ECR `prod-sha-...-arm64-...` image URI를 선택하고 execution role, VPC, protocol, 환경 변수가
   기존 값과 같은지 전부 확인한다.

5. 저장하면 새 Runtime 버전이 만들어진다. 그 버전이 `READY`가 될 때까지 기다린다.

6. **Endpoints** 표에서 `prod`를 선택하고 **Edit**을 누른다.

7. Associated version을 새 `READY` 버전으로 바꾸고 저장한다.

8. Endpoint가 `READY`가 된 뒤 실제 App Server 테스트 작업으로 검증한다.

Runtime 갱신은 부분 수정이 아니라 새 버전 생성이다. 환경 변수나 VPC 설정이 비어 있지
않은지 저장 전에 반드시 확인한다.

## 10. AWS 콘솔에서 롤백하기

롤백은 이미지를 다시 빌드하지 않고 `prod` Endpoint가 가리키는 Runtime 버전만 바꾼다.

1. AgentCore 콘솔 → `laimory_ai` → **Versions**에서 이전 버전이 `READY`인지 확인한다.

2. **Endpoints** 표에서 `prod`를 선택하고 **Edit**을 누른다.

3. Associated version을 직전의 검증된 버전으로 바꾼다.

4. 저장 후 Endpoint가 `READY`가 될 때까지 기다린다.

5. `prod`의 live version이 선택한 값인지 확인한다.

6. 실제 App Server 테스트 작업과 CloudWatch Logs로 복구를 확인한다.

`DEFAULT` Endpoint가 아니라 반드시 `prod`를 바꾼다. 배포 직전 버전을 모르면 GitHub Actions
실행 요약이나 Runtime의 Versions 목록에서 image URI와 생성 시각을 대조한다.

GitHub Actions의 **Rollback Production**도 사용할 수 있다. `main` 브랜치에서 버전을 비워
실행하면 목록만 출력하고, 버전을 입력하면 같은 Endpoint 전환을 자동으로 수행한다.

## 11. 로컬 Docker 검증

`Dockerfile`은 플랫폼을 고정하지 않는다. AgentCore와 같은 arm64 이미지를 확인하려면
다음 명령을 사용한다. x86 개발기에서는 QEMU 에뮬레이션 때문에 느릴 수 있다.

```bash
docker buildx build --platform linux/arm64 --load -t laimory-ai:local .
docker run --rm -p 8080:8080 --name laimory-ai laimory-ai:local
```

다른 터미널에서 확인한다.

```bash
curl http://127.0.0.1:8080/ping
# {"status":"Healthy"}

docker image inspect laimory-ai:local --format '{{.Architecture}}'
# arm64
```

실제 처리까지 확인할 때만 `.env`를 실행 시점에 전달한다. `.env`는 `.dockerignore`에 의해
이미지에 들어가지 않는다.

```bash
docker run --rm -p 8080:8080 --env-file .env laimory-ai:local
```

## 12. 트러블슈팅

| 증상 | AWS 콘솔에서 확인할 곳과 조치 |
|---|---|
| `Environment 'production'의 변수/시크릿이 비어 있다` | `PROD_ECR_REPOSITORY`, `AGENTCORE_RUNTIME_ID`, `AGENTCORE_ENDPOINT_NAME`을 저장소 변수가 아니라 Environment `production`에 등록했는지 확인한다 |
| `AccessDenied`로 ECR push 실패 | IAM → `laimory-ai-github-deploy`의 ECR 정책과 OIDC trust의 organization/repository/branch를 확인한다 |
| `AccessDenied`와 `iam:PassRole` 표시 | 콘솔 작업자 또는 GitHub 배포 역할이 `laimory-ai-agentcore-runtime`을 AgentCore에 전달할 수 있는지 확인한다 |
| service-linked role 생성 실패 | 콘솔 작업자에 `iam:CreateServiceLinkedRole` 권한이 있는지 확인한다 |
| subnet 선택 또는 Runtime 생성 실패 | subnet의 AZ ID가 `apne2-az1`, `apne2-az2`, `apne2-az3` 중 하나인지 확인한다 |
| Runtime `CREATE_FAILED` / `UPDATE_FAILED` | Runtime 버전 세부 화면의 failure reason을 본다. arm64 이미지, 포트 8080, `/ping`, 실행 역할 ECR pull 권한 순으로 확인한다 |
| `Architecture incompatible` / `Supported platforms: [arm64]` | `-amd64-run-...` 태그가 붙은 EC2용 이미지를 선택했다. `laimory-ai-prod`의 `prod-sha-...-arm64-...` 이미지 URI로 새 Runtime 버전을 만든다 |
| ECR image pull timeout | ECR DKR/API interface endpoint, S3 gateway endpoint, endpoint security group과 route table을 확인한다 |
| Bedrock 호출 timeout | NAT 경로나 `bedrock-runtime` interface endpoint와 private DNS를 확인한다 |
| App Server에서 AgentCore 호출 timeout | `bedrock-agentcore` data plane VPC endpoint의 private DNS, App Server outbound `443`, endpoint security group inbound `443`을 확인한다 |
| App Server에서 AgentCore 호출 `403` | EC2 Instance Role과 VPC endpoint policy가 Runtime ARN과 `prod` Runtime Endpoint ARN을 모두 허용하는지 확인한다 |
| App Server 호출 timeout | `APP_SERVER_API_URL`, subnet route, Runtime outbound, App Server inbound와 사설 DNS를 확인한다. DB security group은 관련 없다 |
| CloudWatch 로그가 비어 있음 | Runtime 실행 역할의 Logs 권한과 `logs` interface endpoint 또는 NAT 경로를 확인한다 |
| Langfuse trace가 없음 | `LANGFUSE_ENABLED=true`, key pair, `LANGFUSE_BASE_URL`을 확인하고 Runtime private subnet의 NAT route, DNS, outbound `443`을 확인한다. 키가 불완전하면 애플리케이션은 Langfuse를 no-op으로 비활성화한다 |
| CloudWatch에는 있지만 Elasticsearch에는 없음 | Runtime이 아니라 CloudWatch subscription filter와 전달 Lambda를 본다. filter pattern, Lambda 오류, `AWS/Logs`의 `DeliveryErrors`·`DeliveryThrottling`, Secrets Manager 권한, ES 인증과 Lambda→ES 경로를 확인한다 |
| Elasticsearch에 일반 로그나 민감 필드가 들어감 | 구독을 즉시 중지하고 전달 Lambda가 `event.dataset == "laimory.api"`를 재검사하며 Filebeat 템플릿과 같은 `drop_fields`를 적용하는지 확인한다 |
| 배포 후 환경 변수가 사라짐 | Runtime 갱신 시 전체 설정을 보존하지 않은 경우다. 직전 버전 설정을 대조해 다시 새 버전을 만든다 |
| 백그라운드 처리가 중간에 끊김 | uvicorn worker를 늘리지 않았는지 확인한다. 반드시 단일 worker를 유지한다 |
| 호출은 `202`인데 최종 실패 | CloudWatch에서 같은 `taskId`를 찾고 App Server 입력 조회·결과 저장·콜백 연결을 확인한다. taskToken 값 자체는 검색하거나 로그에 남기지 않는다 |

## 13. 보안과 운영 주의사항

- **환경 변수에 넣은 값은 `GetAgentRuntime`을 호출할 수 있는 사람에게 평문으로 보인다.**
  비밀이 아닌 실행 설정은 Runtime 환경 변수에 두고, `LANGFUSE_SECRET_KEY` 같은 실제
  비밀값만 `SECRETS_BUNDLE_NAME`이 가리키는 Secrets Manager Secret에 둔다.

- Elasticsearch API key는 Runtime에 넣지 않는다. 전달 Lambda만 Secrets Manager에서 읽고,
  운영 이벤트 수집에 필요한 data stream 권한만 갖는다.

- Runtime 실행 역할의 Bedrock `Resource: "*"`는 최초 배포 성공 후 실제 모델과 추론
  프로필 ARN으로 좁힌다.

- GitHub OIDC 신뢰 조건은 `soma17th-369/Laimory-AI`의 `dev` 브랜치로 유지한다.

- `sha-...` 태그와 Runtime 버전, Endpoint live version을 한 묶음으로 기록한다.

- 배포 워크플로에는 수동 승인 gate가 없다. 필요하면 GitHub Environment reviewer를
  별도로 설정한다.

- AgentCore 콘솔 메뉴명은 AWS 업데이트로 달라질 수 있다. 화면이 문서와 다르면
  [AgentCore 개발자 가이드](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)의
  Console 탭을 기준으로 판단한다.
