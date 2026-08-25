# 배포·환경

## Scope

Docker image, branch별 배포 대상(dev=EC2 / main=AgentCore), production 승격 경계, health·idle·rollback·image 보존과 환경 설정 경계를 설명한다.

## Read When

- Dockerfile, GitHub Actions, EC2 deploy script를 바꿀 때
- port, worker, health, architecture, runtime env를 바꿀 때
- 배포·rollback 실패를 진단할 때

## Authoritative Sources

- `Dockerfile`, `.dockerignore`
- `.github/workflows/deploy-ec2.yml`, `deploy-production.yml`, `rollback-production.yml`, `pr-main-guard.yml`
- `docs/deploy-production.md`, `docs/github/main-ruleset.example.json`
- `scripts/deploy-ec2.sh`, `scripts/prune_ecr_images.py`
- `app/api/agentcore.py`, `app/core/inflight.py`, `app/core/config.py`
- `tests/scripts/**`, `tests/api/test_agentcore_endpoint.py`, `tests/api/test_server_lifecycle.py`

## Current Implementation

### Image

한 Dockerfile을 EC2 `linux/amd64`와 AgentCore `linux/arm64`에 사용하고 platform은 workflow가 결정한다. `linux/arm64`, host `0.0.0.0`, port `8080`은 AgentCore Runtime service contract가 요구하는 값이라 선택 사항이 아니다. builder는 pinned uv image에서 `uv sync --locked --no-dev`로 `.venv`를 만들고 runtime은 `python:3.14-slim` 위에 dependency, `app/`, version 조회용 `pyproject.toml`만 복사한다.

`.dockerignore`는 deny-all 뒤 `app`, `pyproject.toml`, `uv.lock`만 허용하므로 `.env`, data, docs, tests, IDE·cache가 build context에 들어가지 않는다. runtime은 uid/gid 10001 non-root이며 app path 쓰기 권한을 주지 않는다.

image default는 prod/json/bedrock/v1이고 환경별 model, App Server URL, Langfuse credential은 runtime environment가 덮어쓴다. healthcheck는 추가 HTTP tool 없이 raw socket으로 8080 `/ping`의 200을 확인한다. Uvicorn worker option을 추가하지 않는다.

### branch별 배포 대상

배포 경로가 branch로 갈린다. `dev`는 개발(EC2), `main`은 production(AgentCore)이며 workflow, ECR repository, IAM role, 변수·secret이 전부 분리돼 있다. 두 경로가 공유하는 것은 `AWS_REGION`과 Dockerfile뿐이다.

### 개발 EC2 배포

`deploy-ec2.yml`은 `dev` branch push 중 app/image/deploy 관련 path 변경 또는 수동 실행으로 동작한다. `dev` 이외 branch에서는 첫 step이 실행을 중단한다. OIDC로 AWS role을 받고 amd64 image를 ECR에 push한 뒤 SSM Run Command로 EC2의 `scripts/deploy-ec2.sh`를 실행한다. tag에는 commit short SHA, workflow run ID와 attempt가 들어가 재실행이 기존 tag를 덮어쓰지 않는다.

EC2에는 앱 `runtime.env`, Filebeat 설정·env·registry data가 GitHub 밖 `/opt/laimory-ai`에 있어야 한다. 장기 AWS access key 대신 instance role과 GitHub OIDC를 사용한다.

비밀과 환경별 값은 `SECRETS_BUNDLE_NAME`이 가리키는 Secrets Manager 시크릿 하나에 JSON 객체로 둔다(#30). 이름이 비면 AWS를 호출하지 않는다. 번들은 `Settings`의 값 공급원으로 붙어 **환경변수·`.env`보다 우선**하고, 번들에 없는 키만 환경변수 → `.env` → `config.py` 기본값으로 내려간다. dev는 EC2 `runtime.env` + 번들, prod는 AgentCore 환경 변수 + 번들이며 고정값은 코드 기본값이다. `SECRETS_BUNDLE_NAME`과 `AGENT_VERSION`은 번들에 넣지 않는다(부트스트랩·배포 주입값). 번들은 기동 시 1회 읽고 변경은 재시작으로 반영한다. 조회 실패는 1408로 남고 그 값들만 없는 것으로 보므로, 필수 값을 번들에만 두면 조회 실패가 기동 실패가 된다. 실행 역할에는 해당 secret ARN의 `secretsmanager:GetSecretValue`가 필요하다.

deploy script는 architecture와 env file, Docker daemon을 확인하고 image를 pull한다. 앱 교체 전에 Filebeat를 확인·기동하되 Filebeat 실패는 앱 배포를 중단하지 않는다. 기존 앱 `/ping`이 `HealthyBusy`이면 10초 간격으로 최대 20분 기다리고, 알 수 없는 상태면 교체를 중단한다.

기존 image URI를 기록한 뒤 컨테이너를 교체한다. 새 container가 정해진 시간 안에 `Healthy` 또는 `HealthyBusy`가 아니면 새 container 로그를 남기고 직전 image로 자동 복구를 시도한다. 성공 뒤 현재 image와 실제 직전 image만 보존하도록 ECR을 정리한다.

### production 배포 (main → AgentCore)

`deploy-production.yml`은 `main` push와 수동 실행으로 동작하고 path filter가 없다. job 하나가 `environment: production`을 선언해 승인 gate와 전용 자격증명이 같은 자리에 있다. arm64 image를 **이동 tag 없이** immutable tag 하나로 `laimory-ai-prod`에 push하고, 현재 Runtime 설정을 읽어 role, network, protocol, environment를 보존한 채 image URI만 바꿔 새 version을 만든다. Runtime과 endpoint READY를 기다린 뒤 endpoint를 새 version으로 전환한다.

ECR push 자체는 배포가 아니다. AgentCore는 ECR을 감시하지 않고, `UpdateAgentRuntime`으로 만든 version을 `UpdateAgentRuntimeEndpoint`가 가리켜야 반영된다. "무엇이 떠 있는가"의 정본은 endpoint → version → containerUri 사슬이다.

치명 health gate는 Runtime READY와 endpoint READY 두 개이며, 둘 다 AgentCore가 container `GET /ping`으로 판정한다. 그 뒤의 `InvokeAgentRuntime` smoke는 `continue-on-error`로 요약에만 남긴다. endpoint 전환 이후 실패하면 기록해 둔 직전 live version으로 자동 복구하고 job을 실패시킨다.

rollback workflow는 재build하지 않고 입력받은 기존 Runtime version으로 endpoint를 전환한다. deploy와 rollback은 같은 concurrency group을 사용해 동시에 endpoint를 바꾸지 않는다.

### production 승격 경계

`main` 직접 push 차단과 PR 필수는 GitHub ruleset이, source branch가 `dev`인지는 `pr-main-guard.yml`이 검사한다. 두 번째는 ruleset의 required status check로 등록해야 merge를 막는다. check 이름은 job `name` 문자열이므로 workflow와 ruleset이 함께 움직여야 한다. ruleset·Environment·변수·secret·IAM은 저장소가 아니라 GitHub·AWS 설정이며 `docs/deploy-production.md`가 절차를 갖는다.

Environment 값은 같은 이름의 저장소 값을 덮어쓰지만, 등록 누락 시 저장소 값이 조용히 쓰인다. 그래서 ECR repository만 `PROD_ECR_REPOSITORY`로 가른다 — 이름을 공유하면 등록 누락이 곧 개발 repository로의 오배포이고, 그 image는 다음 dev 배포에 지워진다.

IAM 배포 role은 dev와 **공용**이다(`AWS_DEPLOY_ROLE_ARN`, 저장소 수준). 그 role의 trust condition에 `ref:refs/heads/dev`와 `environment:production`이 모두 들어가고 권한은 두 경로의 합집합이므로, **role 자체는 권한 경계가 아니다.** 경계는 ECR repository 분리, Environment 승인 gate, deployment branch 정책, workflow별 실행 branch guard가 만든다.

## Invariants

- production 운영 경로는 `main` push → AgentCore Runtime이고 `dev` push → EC2는 개발 경로다.
- EC2는 amd64, AgentCore는 arm64다.
- 두 경로는 ECR repository를 공유하지 않는다. `prune_ecr_images.py`가 repository 전체에서 EC2 배포 tag가 없는 image를 architecture 구분 없이 지우므로, 공유하면 dev 배포가 production image를 삭제한다.
- production image에 이동 tag를 붙이지 않는다. Runtime version과 image가 1:1이어야 rollback이 성립한다.
- production repository에는 정리 step도 lifecycle policy도 두지 않는다. AgentCore는 cold start마다 image를 pull하므로 version이 살아 있는 동안 image가 남아 있어야 한다.
- AWS를 만지는 production job은 반드시 `environment: production` 아래 둔다.
- image에 environment-specific secret·URL을 굽지 않는다.
- 앱 container name과 8080 `/ping` 계약을 배포 script·Filebeat와 함께 바꾼다.
- busy container를 강제 교체하지 않고 idle을 기다린다.
- AgentCore update 시 기존 environment variables를 log에 출력하지 않고 보존한다.
- ECR 정리는 배포 성공 뒤 현재·직전 rollback 후보를 확인한 다음 수행한다.

## Known Gaps

- 배포 전 unit/integration test gate가 workflow에 없다.
- EC2(개발) workflow에는 수동 승인 environment gate가 없다. production에는 있다.
- AgentCore Runtime과 endpoint가 AWS에 아직 없고 `main` branch·ruleset·Environment도 미적용이라, production workflow는 저장소에만 있고 실행된 적이 없다.
- AgentCore가 containerUri의 tag를 version 생성 시점에 digest로 고정하는지 cold start마다 재해석하는지는 저장소만으로 확인할 수 없다. 이동 tag를 쓰지 않아 두 경우의 결과가 같아지도록 회피하고 있다.
- idle task가 20분을 넘으면 배포가 실패하며 drain·task migration 기능은 없다.
- Filebeat 실패는 서비스 배포 성공과 분리돼 있어 애플리케이션은 올라가도 운영 이벤트가 수집되지 않을 수 있다.
- AgentCore가 언제 기본 경로로 복귀할지 판단하는 자동 health/cutover 기준은 저장소에 없다.

## Update When

배포 대상과 branch 대응, branch/path trigger, platform, image contents/default, port/worker/health, env·secret 위치와 이름 규칙, 승인 gate, ECR repository 경계와 retention, idle wait, rollback, Filebeat lifecycle, AgentCore version 전환, production 승격 규칙(ruleset·required check)이 바뀔 때 갱신한다.

## Validation

- `uv run pytest tests/scripts tests/api/test_agentcore_endpoint.py tests/api/test_server_lifecycle.py -q`
- `docker build -t laimory-ai:local .`
- `docker run --rm -p 8080:8080 --env-file <safe-local-env> laimory-ai:local` 후 `/ping`
- Bash 환경에서 `bash -n scripts/deploy-ec2.sh`
- `rg -n "branches:|paths:|workflow_dispatch|platforms:|concurrency:|environment:" .github/workflows`
- `uv run pytest tests/scripts/test_workflow_contracts.py -q`

