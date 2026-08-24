# 배포·환경

## Scope

Docker image, 기본 EC2 배포, AgentCore 수동 복구 경로, health·idle·rollback·image 보존과 환경 설정 경계를 설명한다.

## Read When

- Dockerfile, GitHub Actions, EC2 deploy script를 바꿀 때
- port, worker, health, architecture, runtime env를 바꿀 때
- 배포·rollback 실패를 진단할 때

## Authoritative Sources

- `Dockerfile`, `.dockerignore`
- `.github/workflows/deploy-ec2.yml`, `deploy-agentcore.yml`, `rollback-agentcore.yml`
- `scripts/deploy-ec2.sh`, `scripts/prune_ecr_images.py`
- `app/api/agentcore.py`, `app/core/inflight.py`, `app/core/config.py`
- `tests/scripts/**`, `tests/api/test_agentcore_endpoint.py`, `tests/api/test_server_lifecycle.py`

## Current Implementation

### Image

한 Dockerfile을 EC2 `linux/amd64`와 AgentCore `linux/arm64`에 사용하고 platform은 workflow가 결정한다. builder는 pinned uv image에서 `uv sync --locked --no-dev`로 `.venv`를 만들고 runtime은 `python:3.14-slim` 위에 dependency, `app/`, version 조회용 `pyproject.toml`만 복사한다.

`.dockerignore`는 deny-all 뒤 `app`, `pyproject.toml`, `uv.lock`만 허용하므로 `.env`, data, docs, tests, IDE·cache가 build context에 들어가지 않는다. runtime은 uid/gid 10001 non-root이며 app path 쓰기 권한을 주지 않는다.

image default는 prod/json/bedrock/v1이고 환경별 model, App Server URL, Langfuse credential은 runtime environment가 덮어쓴다. healthcheck는 추가 HTTP tool 없이 raw socket으로 8080 `/ping`의 200을 확인한다. Uvicorn worker option을 추가하지 않는다.

### 기본 EC2 배포

`deploy-ec2.yml`은 `dev` branch push 중 app/image/deploy 관련 path 변경 또는 수동 실행으로 동작한다. OIDC로 AWS role을 받고 amd64 image를 ECR에 push한 뒤 SSM Run Command로 EC2의 `scripts/deploy-ec2.sh`를 실행한다. tag에는 commit short SHA, workflow run ID와 attempt가 들어가 재실행이 기존 tag를 덮어쓰지 않는다.

EC2에는 앱 `runtime.env`, Filebeat 설정·env·registry data가 GitHub 밖 `/opt/laimory-ai`에 있어야 한다. 장기 AWS access key 대신 instance role과 GitHub OIDC를 사용한다.

키 계열 값은 `SECRETS_BUNDLE_NAME`이 가리키는 Secrets Manager 시크릿 하나에 JSON 객체로 둘 수 있다(#30). 이름이 비면 AWS를 호출하지 않는다. **환경변수가 번들보다 우선**하므로 번들로 옮긴 키는 `runtime.env`와 Runtime `environmentVariables`에서 지워야 하며, `APP_ENV=prod`에서 남아 있으면 기동 로그가 경고한다. 조회 실패는 기동을 막지 않고 1408로 남는다. 번들을 쓰는 환경의 실행 역할에는 해당 secret ARN의 `secretsmanager:GetSecretValue`가 필요하다.

deploy script는 architecture와 env file, Docker daemon을 확인하고 image를 pull한다. 앱 교체 전에 Filebeat를 확인·기동하되 Filebeat 실패는 앱 배포를 중단하지 않는다. 기존 앱 `/ping`이 `HealthyBusy`이면 10초 간격으로 최대 20분 기다리고, 알 수 없는 상태면 교체를 중단한다.

기존 image URI를 기록한 뒤 컨테이너를 교체한다. 새 container가 정해진 시간 안에 `Healthy` 또는 `HealthyBusy`가 아니면 새 container 로그를 남기고 직전 image로 자동 복구를 시도한다. 성공 뒤 현재 image와 실제 직전 image만 보존하도록 ECR을 정리한다.

### AgentCore 수동 경로

AgentCore deploy는 `workflow_dispatch`만 지원하는 수동 복구 경로다. arm64 image를 immutable SHA tag와 이동 `dev` tag로 push하고 현재 Runtime 설정을 읽어 role, network, protocol, environment를 보존한 채 image URI만 바꿔 새 version을 만든다. Runtime과 endpoint READY를 기다린 뒤 endpoint를 새 version으로 전환한다.

rollback workflow는 재build하지 않고 입력받은 기존 Runtime version으로 endpoint를 전환한다. deploy와 rollback은 같은 concurrency group을 사용해 동시에 endpoint를 바꾸지 않는다.

## Invariants

- 기본 운영 경로는 EC2 단일 컨테이너이며 AgentCore는 수동 경로다.
- EC2는 amd64, AgentCore는 arm64다.
- image에 environment-specific secret·URL을 굽지 않는다.
- 앱 container name과 8080 `/ping` 계약을 배포 script·Filebeat와 함께 바꾼다.
- busy container를 강제 교체하지 않고 idle을 기다린다.
- AgentCore update 시 기존 environment variables를 log에 출력하지 않고 보존한다.
- ECR 정리는 배포 성공 뒤 현재·직전 rollback 후보를 확인한 다음 수행한다.

## Known Gaps

- 배포 전 unit/integration test gate가 workflow에 없다.
- EC2 workflow에는 별도 수동 승인 environment gate가 없다.
- idle task가 20분을 넘으면 배포가 실패하며 drain·task migration 기능은 없다.
- Filebeat 실패는 서비스 배포 성공과 분리돼 있어 애플리케이션은 올라가도 운영 이벤트가 수집되지 않을 수 있다.
- AgentCore가 언제 기본 경로로 복귀할지 판단하는 자동 health/cutover 기준은 저장소에 없다.

## Update When

기본 배포 대상, branch/path trigger, platform, image contents/default, port/worker/health, env·secret 위치, idle wait, rollback, Filebeat lifecycle, ECR retention, AgentCore version 전환이 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/scripts tests/api/test_agentcore_endpoint.py tests/api/test_server_lifecycle.py -q`
- `docker build -t laimory-ai:local .`
- `docker run --rm -p 8080:8080 --env-file <safe-local-env> laimory-ai:local` 후 `/ping`
- Bash 환경에서 `bash -n scripts/deploy-ec2.sh`
- `rg -n "branches:|paths:|workflow_dispatch|platforms:|concurrency:" .github/workflows`

