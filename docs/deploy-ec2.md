# EC2 컨테이너 배포 가이드

> 기준일: 2026-07-24
> 대상: 기존 `t3.micro` EC2에서 Laimory AI 서버를 단일 Docker 컨테이너로 운영

AgentCore 장애 우회용 운영 경로다. `dev` 브랜치에 배포 대상 파일이 반영되면 GitHub
Actions가 `linux/amd64` 이미지를 ECR에 올리고, AWS Systems Manager Run Command로
EC2의 컨테이너를 교체한다. AgentCore 워크플로는 자동 실행하지 않고 수동 복구 경로로
남긴다.

## 1. 배포 구조

```text
dev push
→ GitHub Actions (deploy-ec2.yml)
→ linux/amd64 이미지 빌드
→ ECR push
→ SSM Run Command
→ EC2가 Instance Role로 ECR pull
→ 기존 작업이 끝날 때까지 대기
→ 컨테이너 교체
→ GET /ping 검증
→ 실패 시 직전 이미지 자동 복구
```

App Server는 AgentCore API 대신 같은 VPC 안의 EC2 HTTP API를 직접 호출한다.

```text
POST http://<EC2_PRIVATE_IP>:8080/v1/timeline
```

`POST /invocations`도 같은 처리를 하지만 AgentCore 호환 어댑터이므로, 새 App Server
구현은 일반 API인 `/v1/timeline`을 사용한다.

## 2. 이미지 아키텍처

`t3.micro`는 x86_64이므로 EC2 워크플로는 `linux/amd64`를 빌드한다. Dockerfile에는
플랫폼을 고정하지 않는다.

| 워크플로 | 플랫폼 | 실행 방식 |
|---|---|---|
| `deploy-ec2.yml` | `linux/amd64` | `t3.micro` Docker |
| `deploy-agentcore.yml` | `linux/arm64` | AgentCore 수동 복구 |

EC2 태그는 같은 커밋의 재실행도 덮어쓰지 않도록 아래처럼 만든다.

```text
sha-<커밋12자리>-amd64-run-<GitHub run id>-<attempt>
```

## 3. GitHub 저장소 설정

저장소의 **Settings → Secrets and variables → Actions**에서 설정한다.

| 이름 | 종류 | 값 |
|---|---|---|
| `AWS_REGION` | Variable | `ap-northeast-2` |
| `ECR_REPOSITORY` | Variable | `laimory-ai` |
| `EC2_INSTANCE_ID` | Variable | 대상 EC2의 `i-...` |
| `AWS_DEPLOY_ROLE_ARN` | Secret | 기존 GitHub OIDC 배포 Role ARN |

AgentCore용 `AGENTCORE_RUNTIME_ID`, `AGENTCORE_ENDPOINT_NAME`은 남겨도 EC2
워크플로에서 읽지 않는다.

## 4. GitHub 배포 Role

기존 `laimory-ai-github-deploy` Role의 신뢰 정책과 AgentCore 권한은 유지한다.
EC2 배포에는 아래 두 문장만 추가한다. `instance/*`는 실제 Instance ID를 받은 뒤
해당 인스턴스 ARN 하나로 좁히는 것을 권장한다.

```json
{
  "Sid": "SsmSendDeployCommand",
  "Effect": "Allow",
  "Action": "ssm:SendCommand",
  "Resource": [
    "arn:aws:ssm:ap-northeast-2::document/AWS-RunShellScript",
    "arn:aws:ec2:ap-northeast-2:392900063927:instance/*"
  ]
},
{
  "Sid": "SsmReadDeployResult",
  "Effect": "Allow",
  "Action": "ssm:GetCommandInvocation",
  "Resource": "*"
}
```

이 Role은 이미지를 ECR에 **push**하고 EC2에 배포 명령을 보낸다. EC2가 이미지를
pull하거나 Bedrock을 호출할 때 이 Role을 쓰는 것은 아니다.

## 5. EC2 Instance Role

EC2에는 별도의 Instance Role이 연결돼 있어야 한다. 예시 이름:

```text
laimory-ai-ec2-runtime
```

Role의 신뢰할 서비스는 `ec2.amazonaws.com`이다. 다음 AWS 관리형 정책을 연결한다.

```text
AmazonSSMManagedInstanceCore
```

그리고 다음 인라인 정책을 연결한다.

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
      "Sid": "EcrPull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:ap-northeast-2:392900063927:repository/laimory-ai"
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

EC2 콘솔에서 **인스턴스 선택 → 작업 → 보안 → IAM 역할 수정**으로 연결한다.
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`는 EC2나 env 파일에 넣지 않는다.

## 6. 네트워크

EC2와 staging DB는 같은 VPC에서 통신해야 한다.

### EC2 Security Group 인바운드

| 포트 | 소스 |
|---|---|
| TCP `8080` | App Server Security Group |

`8080`을 `0.0.0.0/0`에 열지 않는다. 현재 애플리케이션 API에는 인터넷 공개용 인증
계층이 없다.

### DB Security Group 인바운드

| 포트 | 소스 |
|---|---|
| TCP `3306` | EC2 Security Group |

EC2는 ECR, Systems Manager, Bedrock, App Server 콜백 URL로 나갈 수 있어야 한다.
private subnet이고 NAT가 없다면 각 서비스의 VPC Endpoint 구성이 필요하다.

## 7. EC2 최초 준비

Session Manager로 접속해 아키텍처와 운영체제를 확인한다.

```bash
uname -m
cat /etc/os-release
free -h
df -h
```

`t3.micro`의 예상 아키텍처는 `x86_64`다. Amazon Linux 2023에서 Docker를 준비한다.

```bash
sudo dnf update -y
sudo dnf install -y docker
sudo systemctl enable --now docker
```

SSM Agent와 Instance Role을 확인한다.

```bash
sudo systemctl status amazon-ssm-agent
aws sts get-caller-identity
```

## 8. 운영 환경변수

환경변수는 GitHub에 올리지 않고 EC2에 아래 파일로 한 번만 준비한다.

```text
/opt/laimory-ai/runtime.env
```

```bash
sudo mkdir -p /opt/laimory-ai
sudo touch /opt/laimory-ai/runtime.env
sudo chmod 600 /opt/laimory-ai/runtime.env
sudo vi /opt/laimory-ai/runtime.env
```

예시:

```dotenv
APP_ENV=prod
LOG_LEVEL=INFO
LOG_FORMAT=json

LLM_PROVIDER=bedrock
BEDROCK_MODEL=<모델 또는 추론 프로필 ID>
BEDROCK_REGION=ap-northeast-2
BEDROCK_AWS_PROFILE=

DB_HOST=<staging DB private host>
DB_PORT=3306
DB_NAME=<DB 이름>
DB_USER=<DB 사용자>
DB_PASSWORD=<DB 비밀번호>

APP_SERVER_API_URL=<App Server 서버간 API 기본 URL, 예: https://api.example.com/s/api/v1>
```

`BEDROCK_AWS_PROFILE`은 비워 둔다. boto3가 EC2 Instance Role의 임시 자격증명을
사용한다.

기존 `CALLBACK_URL`은 사용하지 않는다. `APP_SERVER_API_URL`에는 task별 경로를
제외하고 `/s/api/v1`까지 넣는다. AI 서버가
`/timeline/drafts/{taskId}/callback`을 붙여 호출한다.

## 9. 배포

자동 배포 대상:

- `app/**`
- `Dockerfile`, `.dockerignore`
- `pyproject.toml`, `uv.lock`
- `scripts/deploy-ec2.sh`
- `.github/workflows/deploy-ec2.yml`

수동 배포는 Actions의 **Deploy EC2 → Run workflow**에서 실행한다.

배포 스크립트는 기존 컨테이너의 `/ping`이 `HealthyBusy`면 최대 20분 동안 기다린다.
유휴 상태가 된 뒤 이미지를 교체하고 5분 동안 새 컨테이너를 확인한다. 새 컨테이너가
정상 기동하지 못하면 직전 이미지로 자동 복구한다.

## 10. t3.micro 운영 주의

`t3.micro`는 메모리가 1GiB라 긴급 운영 용도로만 본다.

- uvicorn worker는 하나만 사용한다.
- 이미지는 GitHub Actions에서 빌드하고 EC2에서는 pull만 한다.
- `free -h`, `docker stats`, 커널 OOM 로그를 관찰한다.
- 메모리 부족이 반복되면 `t3.small` 이상으로 변경한다.

상태 확인:

```bash
curl http://127.0.0.1:8080/ping
docker ps --filter name=laimory-ai
docker logs --tail 200 laimory-ai
```
