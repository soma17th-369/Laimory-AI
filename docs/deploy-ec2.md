# EC2 컨테이너 배포 가이드

> 기준일: 2026-07-31 (이슈 #47 로그 수집기 반영)
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
→ 로그 수집기(laimory-filebeat) 확인·기동
→ 기존 작업이 끝날 때까지 대기
→ 애플리케이션 컨테이너 교체
→ GET /ping 검증
→ 실패 시 직전 이미지 자동 복구
```

EC2에는 컨테이너가 둘이다.

| 컨테이너 | 역할 | 배포 시 |
|---|---|---|
| `laimory-ai` | 애플리케이션 | 매번 새 이미지로 교체 |
| `laimory-filebeat` | 운영 로그 수집(#47). 앱 stdout → Elasticsearch | 정상 동작 중이면 **건드리지 않는다** |

앱 교체 중에 수집기까지 내리면 그 사이 로그를 잃으므로, 이미지와 설정이 그대로면
Filebeat는 그대로 둔다. 자세한 로그 계약은
[operational-logging.md](operational-logging.md) 참고.

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
EC2 배포에는 아래 세 문장을 추가한다. `instance/*`는 실제 Instance ID를 받은 뒤
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
},
{
  "Sid": "EcrPruneOldImages",
  "Effect": "Allow",
  "Action": [
    "ecr:DescribeImages",
    "ecr:BatchDeleteImage"
  ],
  "Resource": "arn:aws:ecr:ap-northeast-2:392900063927:repository/laimory-ai"
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

EC2와 App Server는 같은 VPC에서 통신해야 한다. AI 서버는 이슈 #40 이후 staging DB에
직접 붙지 않으므로 **DB 네트워크 경로와 자격증명이 필요하지 않다.**

### EC2 Security Group 인바운드

| 포트 | 소스 |
|---|---|
| TCP `8080` | App Server Security Group |

`8080`을 `0.0.0.0/0`에 열지 않는다. 현재 애플리케이션 API에는 인터넷 공개용 인증
계층이 없다.

### App Server Security Group 인바운드

| 포트 | 소스 |
|---|---|
| App Server API 포트 | EC2 Security Group |

AI 서버가 입력 조회·결과 저장·완료 콜백을 모두 App Server 서버간 API로 호출하므로
반대 방향 경로도 열려 있어야 한다.

EC2는 ECR, Systems Manager, Bedrock, App Server API, **Elasticsearch**로 나갈 수
있어야 한다. Elasticsearch로 나가는 것은 애플리케이션이 아니라 같은 인스턴스의
`laimory-filebeat` 컨테이너다(이슈 #47). ES가 VPC 밖이면 그 경로도 열어야 하며,
Filebeat 이미지를 받으려면 `docker.elastic.co`에도 닿아야 한다.

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
# 운영에서는 반드시 json 이다. Filebeat 가 stdout 을 줄 단위로 읽으므로 한 줄이
# 유효한 JSON 이 아니면 그 이벤트를 통째로 잃는다(이슈 #47).
LOG_FORMAT=json

LLM_PROVIDER=bedrock
BEDROCK_MODEL=<모델 또는 추론 프로필 ID>
BEDROCK_REGION=ap-northeast-2
BEDROCK_AWS_PROFILE=

APP_SERVER_API_URL=<App Server 서버간 API 기본 URL, 예: https://api.example.com/s/api/v1>
APP_SERVER_TIMEOUT_SEC=10
APP_SERVER_MAX_ATTEMPTS=3
APP_SERVER_RETRY_BACKOFF_SEC=0.5

# 사진 이미지 다운로드(이슈 #52). 값을 비우면 다운로드를 하지 않고 사진 설명이
# 메타데이터 추정으로만 만들어진다 — 운영에서는 반드시 채운다.
PHOTO_URL_ALLOWED_HOSTS=<이미지 호스트 suffix, 예: <버킷>.s3.ap-northeast-2.amazonaws.com>
PHOTO_DOWNLOAD_TIMEOUT_SEC=5
PHOTO_MAX_IMAGE_BYTES=5242880
PHOTO_MAX_IMAGES=20
PHOTO_MAX_TOTAL_IMAGE_BYTES=20971520
PHOTO_DOWNLOAD_MAX_WORKERS=4
PHOTO_DOWNLOAD_BUDGET_SEC=30

# 선택: Langfuse. 키는 이 파일의 0600 권한과 EC2 접근 정책으로 보호한다.
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
LANGFUSE_SAMPLE_RATE=1.0
LANGFUSE_MAX_PAYLOAD_BYTES=65536
```

`PHOTO_URL_ALLOWED_HOSTS`는 **비워 두면 사진 이미지를 내려받지 않는다**(fail closed).
설정을 깜빡한 배포에서 임의 URL로 요청이 나가는 것보다 설명 품질이 떨어지는 편이
안전하기 때문이다. 값은 호스트 suffix 목록이며 쉼표로 여러 개를 넣을 수 있다.
`example.com`을 넣으면 `example.com`과 `*.example.com`이 허용된다. `https`가 아니거나
allowlist 밖인 URL은 요청조차 보내지 않고, redirect도 따라가지 않는다.

`PHOTO_MAX_TOTAL_IMAGE_BYTES`는 App Server에는 없는 **AI 서버 고유 제한**이다.
App Server는 장당 5MB·요청당 20장을 허용하되 총합을 제한하지 않아 이론상 100MB가
가능한데, 사진 설명은 배치 1회 vision 호출이라 그 전부가 한 요청에 실린다. 실사용
이미지 크기는 Langfuse의 `download-photo-images` 스팬에 남는 `byteLength`로 확인하고
값을 조정한다.

`LANGFUSE_CONTENT_CAPTURE`는 넣지 않는다. 비워 두면 `APP_ENV` 기준으로 local/dev는
`SANITIZED`, 그 밖은 `NONE`이 적용된다. 이 파일에 값을 적으면 그 값이 코드 기본값을
이기므로, **dev 인스턴스의 `runtime.env`에 예전 `LANGFUSE_CONTENT_CAPTURE=NONE` 줄이
남아 있으면 지운다.** 남겨 두면 Langfuse trace에 본문이 계속 보이지 않는다(이슈 #48).

예전 관측 설정(`OBS_ENABLED`, `ES_URL`, `ES_API_KEY`, `ES_EVENT_INDEX`,
`OBS_CONTENT_CAPTURE`, `OBS_LOCAL_DIR` 등)이 남아 있으면 지운다. 이슈 #47 이후
애플리케이션은 이 값을 읽지 않으며(설정이 `extra="ignore"`라 조용히 무시된다),
남겨 두면 앱이 아직 Elasticsearch에 붙는 것처럼 보인다. Elasticsearch 접속정보는
`filebeat.env`(§9) 한 곳에만 둔다.

`AGENT_VERSION`도 이 파일에 넣지 않아도 된다. `scripts/deploy-ec2.sh`가 배포 이미지
태그를 컨테이너에 넘기며, `docker run -e`가 `--env-file`보다 우선한다.

`BEDROCK_AWS_PROFILE`은 비워 둔다. boto3가 EC2 Instance Role의 임시 자격증명을
사용한다.

`APP_SERVER_API_URL`은 **필수**다(이슈 #40). AI 서버의 유일한 데이터 경로이며,
비어 있으면 컨테이너가 기동 시점에 실패한다. task별 경로를 제외하고 버전 경로까지
넣으면(`/s/api/v1` 또는 `/s/v1`) AI 서버가 `/timeline/drafts/{taskId}/input`,
`/result`, `/callback`을 붙여 호출한다. 기존 `CALLBACK_URL`과 `DB_*`는 사용하지 않는다.

## 9. 로그 수집기(Filebeat) 준비

운영 로그는 앱 컨테이너 stdout에서 나와 별도 Filebeat 컨테이너를 거쳐 Elasticsearch로
들어간다(이슈 #47). 애플리케이션은 Elasticsearch에 직접 접속하지 않으므로, ES 접속정보는
**이 EC2에만** 둔다.

### 9.1 파일 세 개와 디렉터리 하나

```bash
sudo mkdir -p /opt/laimory-ai/filebeat-data
```

| 경로 | 내용 | 권한 |
|---|---|---|
| `/opt/laimory-ai/filebeat.yml` | Filebeat 설정. 자격증명은 `${}` 참조만 | root:root `0600` |
| `/opt/laimory-ai/filebeat.env` | `FILEBEAT_IMAGE`, ES 접속정보, 환경 이름 | root:root `0600` |
| `/opt/laimory-ai/filebeat-data/` | registry(읽은 위치). **필수** | root:root `0700` |
| `/opt/laimory-ai/runtime.env` | 앱 환경변수(§8) | root:root `0600` |

`filebeat-data`가 없거나 마운트되지 않으면 Filebeat가 재시작할 때마다 로그를 처음부터
다시 읽어 Elasticsearch에 중복이 쌓인다.

### 9.2 설정 파일

저장소의 [`docs/observability/filebeat.example.yml`](observability/filebeat.example.yml)을
그대로 복사한다. 이 템플릿에는 자격증명이 없다 — 값은 `filebeat.env`에서 환경변수로
들어간다.

```bash
sudo vi /opt/laimory-ai/filebeat.yml    # 템플릿 내용 붙여넣기
sudo chown root:root /opt/laimory-ai/filebeat.yml
sudo chmod 600 /opt/laimory-ai/filebeat.yml
```

수집 대상은 컨테이너 이름이 정확히 `laimory-ai`인 것 하나다. Filebeat 자신의 로그나
다른 컨테이너 로그는 들어오지 않는다.

### 9.3 접속정보

```bash
sudo touch /opt/laimory-ai/filebeat.env
sudo chmod 600 /opt/laimory-ai/filebeat.env
sudo vi /opt/laimory-ai/filebeat.env
```

```dotenv
# Elasticsearch 버전에 맞춘 태그를 쓴다. 배포 스크립트가 이 값으로 컨테이너를 만든다.
FILEBEAT_IMAGE=docker.elastic.co/beats/filebeat:<ES 버전에 맞춘 태그>

ES_HOSTS=https://<elasticsearch-host>:9200
# 수집 전용 API key. 인덱스 쓰기 권한만 준다(템플릿·ILM 관리 권한은 주지 않는다).
ES_API_KEY=<id>:<api_key>

# data stream 이름의 namespace. logs-laimory.ai-<이 값> 으로 들어간다.
LAIMORY_ENV=prod
```

`ES_API_KEY`에는 `logs-laimory.ai-*`에 대한 `auto_configure`/`create_doc` 권한만 주면
된다. Filebeat 설정이 `setup.template.enabled: false`, `setup.ilm.enabled: false`라
관리 권한을 요구하지 않는다. 인덱스 템플릿은 Elasticsearch 내장 `logs-*-*` 템플릿이
잡아 준다.

### 9.4 배포 스크립트가 하는 일

`scripts/deploy-ec2.sh`가 앱 컨테이너를 교체하기 **전에** `ensure_filebeat`를 실행한다.

- 설정 파일이나 `filebeat.env`가 없으면 경고만 남기고 건너뛴다(`FILEBEAT_STATUS=skipped-*`)
- 이미 실행 중이고 이미지·설정 해시가 그대로면 **건드리지 않는다**(`running`)
- 이미지나 설정이 바뀌었거나 죽어 있으면 재생성한다(`started`)
- 기동에 실패해도 **애플리케이션 배포를 실패시키지 않는다**(`failed`)

마지막 항목이 정책이다. 로그 수집이 서비스 가용성을 막는 구조는 만들지 않는다. 대신
GitHub Actions 요약의 `Filebeat` 행과 워크플로 경고로 드러나므로, `failed`나
`skipped-*`가 보이면 배포 자체와 무관하게 따로 조치한다.

컨테이너는 `--restart unless-stopped`로 뜨므로 EC2 재부팅이나 Docker 재시작 후에도
자동으로 복구된다. 두 컨테이너 모두 `--log-opt max-size=20m --log-opt max-file=3`으로
로그를 로테이션한다 — json-file 드라이버 기본값은 무제한이라 `t3.micro` 디스크가
조용히 찬다.

### 9.5 `docker.sock` 마운트에 대해

Filebeat는 컨테이너 이름으로 수집 대상을 고르기 위해 Docker autodiscover를 쓰며, 이를
위해 `/var/run/docker.sock`을 **읽기 전용**으로 마운트한다. Docker 데몬 접근은 사실상
호스트 root 상당 권한이므로, 이 컨테이너에는 신뢰하는 공식 이미지만 쓰고 태그를
고정한다. 이 노출을 허용할 수 없으면 수집 방식을 컨테이너 ID 경로 glob으로 바꿔야
하며, 그 경우 이름 기반 필터의 정확도가 떨어진다.

## 10. 배포

자동 배포 대상:

- `app/**`
- `Dockerfile`, `.dockerignore`
- `pyproject.toml`, `uv.lock`
- `scripts/deploy-ec2.sh`
- `scripts/prune_ecr_images.py`
- `.github/workflows/deploy-ec2.yml`

수동 배포는 Actions의 **Deploy EC2 → Run workflow**에서 실행한다.

배포 스크립트는 기존 컨테이너의 `/ping`이 `HealthyBusy`면 최대 20분 동안 기다린다.
유휴 상태가 된 뒤 이미지를 교체하고 5분 동안 새 컨테이너를 확인한다. 새 컨테이너가
정상 기동하지 못하면 직전 이미지로 자동 복구한다.

`filebeat.yml`을 고쳤을 때는 배포가 설정 해시 변화를 보고 Filebeat를 재생성한다.
앱 변경 없이 수집 설정만 바꾸려면 Actions에서 워크플로를 수동 실행하거나, EC2에서
직접 컨테이너를 지우고 다음 배포를 기다린다.

배포 성공 후 workflow는 ECR에서 현재 배포 이미지와 실제로 교체된 직전 이미지의
태그를 확인하고, 나머지 tagged/untagged 이미지 digest를 삭제한다. 단순 push 시각의
최신 2개가 아니라 실행 중이던 직전 컨테이너 이미지를 보존하므로, 중간에 배포 실패
이미지가 push돼 있어도 롤백 후보가 바뀌지 않는다. 현재 또는 직전 태그를 ECR에서
확인할 수 없으면 안전을 위해 정리를 건너뛰거나 실패 처리하며 기존 이미지를 삭제하지
않는다. `BatchDeleteImage` 제한에 맞춰 한 번에 최대 100개씩 삭제한다.

## 11. 운영과 장애 대응

### 상태 확인

```bash
curl http://127.0.0.1:8080/ping
docker ps --filter name=laimory-
docker logs --tail 200 laimory-ai
docker logs --tail 200 laimory-filebeat
```

애플리케이션 로그는 한 줄 JSON이다. 사람이 읽을 때는 `jq`를 쓴다.

```bash
docker logs --tail 200 laimory-ai | jq -r '"\(.timestamp) \(."log.level") \(.taskId // "-") \(.message)"'
```

### Filebeat가 로그를 보내지 못할 때

앱은 영향받지 않는다. stdout은 계속 나가고 Docker 로그 파일에 남아 있으므로, 수집기를
복구하면 registry에 기록된 위치부터 **이어서** 전송한다.

```bash
docker logs --tail 100 laimory-filebeat        # 인증(401)/연결/권한 오류 확인
docker restart laimory-filebeat                # 대개 이걸로 복구된다
docker inspect laimory-filebeat --format '{{.State.Status}} {{.RestartCount}}'
```

컨테이너가 아예 없으면 다음 배포가 다시 만든다. 즉시 세우려면 Actions에서 워크플로를
수동 실행한다.

### Elasticsearch가 죽었을 때

Filebeat가 자체 큐에 담고 재시도한다. 큐가 차면 harvester가 멈추고, ES가 돌아오면
로그 파일에 남아 있는 지점부터 이어 읽는다. 확인할 것은 **로그 파일이 그 사이에
로테이션으로 사라지지 않았는지**다.

```bash
# 앱 컨테이너 로그 파일 크기와 로테이션 상태
sudo ls -alh /var/lib/docker/containers/$(docker inspect --format '{{.Id}}' laimory-ai)/
```

`max-size=20m × max-file=3` = 최대 60MB치가 버퍼다. 장애가 그보다 오래 가면 오래된
구간은 유실된다. ES 장애가 길어질 것 같으면 로테이션 상한을 임시로 올린다.

### 로그 적체·유실 확인

```bash
# 마지막으로 적재된 시각 (오래 전이면 파이프라인이 끊긴 것)
curl -s "$ES_HOSTS/logs-laimory.ai-prod/_search?size=1&sort=@timestamp:desc" \
  -H "Authorization: ApiKey $ES_API_KEY" | jq '.hits.hits[0]._source."@timestamp"'
```

registry가 커졌는지도 가끔 본다. 설정이 `clean_removed: false`라 배포마다 항목이
하나씩 쌓인다(배포 시 삭제된 컨테이너의 마지막 줄까지 읽기 위한 의도된 선택이다).

```bash
sudo du -sh /opt/laimory-ai/filebeat-data
```

수백 MB가 되면 Filebeat를 멈추고 registry를 지운 뒤 다시 세운다. 이때 현재 파일의
읽은 위치도 함께 사라져 **중복 적재가 한 번 일어난다.**

```bash
docker rm -f laimory-filebeat
sudo rm -rf /opt/laimory-ai/filebeat-data/*
# 다음 배포 또는 워크플로 수동 실행으로 재생성
```

### 롤백

애플리케이션 롤백은 기존과 같다 — 배포 스크립트가 새 컨테이너 기동 실패를 감지하면
직전 이미지로 자동 복구한다. Filebeat는 앱 이미지와 무관하므로 롤백 대상이 아니다.

수집 설정을 되돌려야 하면 `/opt/laimory-ai/filebeat.yml`을 이전 내용으로 되돌리고
컨테이너를 지운다. 다음 배포가 바뀐 해시를 보고 재생성한다.

```bash
docker rm -f laimory-filebeat
```

## 12. t3.micro 운영 주의

`t3.micro`는 메모리가 1GiB라 긴급 운영 용도로만 본다. 컨테이너가 둘이 되면서 여유가
더 줄었다 — Filebeat는 RSS 100~150MB를 쓴다.

- uvicorn worker는 하나만 사용한다.
- 이미지는 GitHub Actions에서 빌드하고 EC2에서는 pull만 한다.
- `free -h`, `docker stats`, 커널 OOM 로그를 관찰한다.
- Filebeat 설정의 `queue.mem`과 `max_procs`를 임의로 올리지 않는다(t3.micro에 맞춰
  줄여 둔 값이다).
- 메모리 부족이 반복되면 `t3.small` 이상으로 변경한다.

```bash
docker stats --no-stream
```
