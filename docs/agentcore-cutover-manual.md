# AgentCore 전환 수동 작업 매뉴얼 (이슈 #89)

> 기준일: 2026-08-24
> 대상: AgentCore Runtime 을 실제 운영 경로로 올리기 위해 **사람이 AWS 콘솔·CLI 와 GitHub
> 설정에서 직접 해야 하는 작업**

이슈 #89 의 코드 몫(호출 계약)은 끝났다. 이 문서는 저장소가 대신할 수 없는 나머지를
순서대로 적는다. 절차의 상세와 IAM 정책 원문은
[AgentCore Runtime 배포 가이드](deploy-agentcore.md)에 있고, 여기서는 **무엇을 어떤
순서로 하고 무엇으로 끝났는지 확인하는가**만 다룬다.

예시의 `123456789012` 는 AWS 계정 번호, 리전은 `ap-northeast-2` 로 가정한다.

## 0. 지금 상태

| 항목 | 상태 |
|---|---|
| 컨테이너 계약 (`POST /invocations`, `GET /ping`) | 완료 |
| `requestType` 호출 계약 (타임라인 + User Memory) | 완료 (#89) |
| arm64 이미지 빌드·Runtime 갱신·엔드포인트 전환 워크플로 | 완료 (#29) |
| 롤백 워크플로 | 완료 (#29) |
| ECR·OIDC·배포 역할·실행 역할 | **확인 필요** (1장) |
| Runtime·전용 엔드포인트 | **미생성** (2장) |
| `AGENTCORE_RUNTIME_ID`·`AGENTCORE_ENDPOINT_NAME` | **미등록** (3장) |
| App Server 의 `InvokeAgentRuntime` 호출 권한 | **미구성** (4장) |
| `main` 기반 production 배포 워크플로·경계 | 완료 (#90) |
| `main` 브랜치·ruleset·Environment `production` | **미적용** ([Production 배포 가이드 §3](deploy-production.md)) |

`dev` push 는 EC2(개발)로, `main` push 는 AgentCore Runtime(운영)으로 나간다(#90).
**이 매뉴얼은 그중 AgentCore 쪽 AWS 자원을 만드는 부분이다.** GitHub 쪽 승격 경계
(`main` 브랜치 생성, ruleset, Environment, production 변수·시크릿)는
[Production 배포 가이드](deploy-production.md)에 있고, 둘 다 끝나야 자동 배포가 돈다.

아래 3장의 저장소 변수는 **Environment `production` 수준**에 등록한다. 저장소 수준에
만들면 등록 누락이 조용한 오배포가 된다(Production 배포 가이드 §3.4).

## 1. 사전 준비 확인

[deploy-agentcore.md 3장](deploy-agentcore.md#3-aws-사전-준비)의 네 가지가 이미 있는지
확인한다. EC2 배포를 쓰고 있었다면 앞의 세 개는 대개 갖춰져 있다.

```bash
# ECR 리포지토리
aws ecr describe-repositories --repository-names laimory-ai --region ap-northeast-2 \
  --query 'repositories[0].repositoryUri' --output text

# OIDC 공급자 (계정당 하나)
aws iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?contains(Arn, 'token.actions.githubusercontent.com')]"

# 배포용 역할 (GitHub Actions 가 맡는 역할)
aws iam get-role --role-name laimory-ai-github-deploy --query 'Role.Arn' --output text

# Runtime 실행 역할 (컨테이너가 쓰는 역할)
aws iam get-role --role-name laimory-ai-agentcore-runtime --query 'Role.Arn' --output text
```

**배포용 역할에 AgentCore 권한이 붙어 있어야 한다.** EC2 배포만 하던 역할이면
`bedrock-agentcore:*` 와 `iam:PassRole` 이 빠져 있다. [3.3 절](deploy-agentcore.md#33-배포용-iam-역할-github-actions-가-맡는-역할)의
`AgentCoreDeploy`·`PassRuntimeRole` 문을 확인한다. `iam:PassRole` 이 없으면 배포가
`AccessDenied` 로 떨어진다.

## 2. Runtime 과 전용 엔드포인트 생성

`AGENTCORE_RUNTIME_ID` 는 Runtime 을 만들어야 생기고, Runtime 은 ECR 에 이미지가 있어야
만들 수 있다. 그래서 **이미지 먼저**다.

**2-1. 이미지를 올린다.** Actions → **Deploy Production** → `Run workflow`.

- `build` job 은 성공하고 arm64 이미지가 ECR 에 올라간다.
- `deploy` job 은 `AGENTCORE_RUNTIME_ID` 가 비어 있어 첫 스텝에서 **의도적으로 멈춘다.**
  `저장소 변수가 비어 있다: AGENTCORE_RUNTIME_ID ...` 가 나오면 정상이다.
- 빌드 요약에 적힌 `sha-<커밋12자>` 태그를 적어 둔다.

**2-2. 환경 변수 파일을 만든다.** [7장 표](deploy-agentcore.md#7-운영-환경-변수)를 보고
`runtime-env.json` 을 만든다. **커밋하지 않는다**(`.gitignore` 에 있다).

필수는 `APP_ENV`, `LOG_LEVEL`, `LLM_PROVIDER`, `BEDROCK_MODEL`, `APP_SERVER_API_URL` 이다.
`APP_SERVER_API_URL` 이 비면 컨테이너가 기동에 실패한다 — AI 서버는 DB 에 직접 접근하지
않고 이 API 하나로만 데이터를 읽고 쓴다. `BEDROCK_AWS_PROFILE` 은 **넣지 않는다**(비어
있어야 실행 역할 자격증명을 쓴다).

**2-3. Runtime 을 만든다.** 네트워크 모드는 **`VPC`** 다. AI 서버가 입력 조회·결과 저장·
콜백을 전부 App Server 서버간 API 로 호출하므로 App Server 에 닿는 경로가 필요하다.

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

> Runtime 이름에는 **하이픈을 못 쓴다**(`[a-zA-Z][a-zA-Z0-9_]{0,47}`). `laimory-ai` ❌ →
> `laimory_ai` ✅. ECR 리포지토리 이름은 규칙이 달라 `laimory-ai` 로 둬도 된다.

`READY` 가 될 때까지 기다린다. `CREATE_FAILED` 면 `failureReason` 을 먼저 본다.

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id laimory_ai-a1B2c3D4e5 --region ap-northeast-2 \
  --query '{status:status,reason:failureReason}'
```

**2-4. 전용 엔드포인트를 만든다.**

```bash
aws bedrock-agentcore-control create-agent-runtime-endpoint \
  --agent-runtime-id laimory_ai-a1B2c3D4e5 \
  --name prod \
  --agent-runtime-version 1 \
  --region ap-northeast-2
```

> **`DEFAULT` 엔드포인트를 쓰지 않는다.** 최신 버전을 따라가도록 동작해서 롤백 지점으로
> 쓸 수 없다. 롤백은 "엔드포인트를 이전 버전에 고정" 하는 방식이라 전용 엔드포인트가
> 반드시 있어야 한다.

## 3. GitHub 변수 등록 (Environment `production`)

저장소 → **Settings** → **Environments** → **production**. Environment 가 아직 없으면
[Production 배포 가이드 §3.3](deploy-production.md) 을 먼저 한다.

| 이름 | 종류 | 값의 출처 |
|---|---|---|
| `AGENTCORE_RUNTIME_ID` | Variable | 2-3 의 `create-agent-runtime` 응답 `agentRuntimeId` |
| `AGENTCORE_ENDPOINT_NAME` | Variable | 2-4 에서 직접 정한 `--name` (예: `prod`) |

```bash
gh variable set AGENTCORE_RUNTIME_ID   --env production --body "laimory_ai-a1B2c3D4e5"
gh variable set AGENTCORE_ENDPOINT_NAME --env production --body "prod"
```

`--env production` 을 빠뜨려 저장소 수준에 만들지 않는다. `PROD_ECR_REPOSITORY` 도 같은
Environment 에 있어야 한다(Production 배포 가이드 §3.4). `AWS_REGION` 과 배포 역할
`AWS_DEPLOY_ROLE_ARN` 은 저장소 수준에서 dev 와 공용으로 쓴다.

## 4. App Server 의 호출 권한과 네트워크

여기부터가 #89 로 새로 필요해진 부분이다. 지금까지의 IAM 은 **배포**(control plane) 권한만
다뤘고, App Server 가 Runtime 을 **호출**(data plane)하는 권한은 없다.

**4-1. Runtime ARN 을 확인한다.** 직접 조립하지 말고 응답에서 가져온다.

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id laimory_ai-a1B2c3D4e5 --region ap-northeast-2 \
  --query 'agentRuntimeArn' --output text
```

**4-2. App Server 실행 주체에 호출 권한을 붙인다.** App Server 가 EC2 면 인스턴스 역할,
ECS 면 태스크 역할이다. `Resource` 에는 4-1 에서 받은 ARN 을 쓴다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeAgentCoreRuntime",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeAgentRuntime",
      "Resource": "<4-1 에서 받은 agentRuntimeArn>"
    }
  ]
}
```

> 엔드포인트를 자원 수준에서 구분해야 하면 `Resource` 에 엔드포인트까지 포함한 ARN 을
> 넣는다. 정확한 표기는 `aws bedrock-agentcore-control get-agent-runtime-endpoint` 응답의
> ARN 필드를 그대로 쓴다. 임의로 문자열을 만들지 않는다.

**장기 액세스 키를 쓰지 않는다.** 인스턴스·태스크 역할로 기본 자격증명 체인을 태우고,
SigV4 서명은 AWS SDK 가 처리한다.

**4-3. 네트워크 두 방향을 확인한다.** 방향이 서로 다르다.

| 방향 | 성격 | 필요한 것 |
|---|---|---|
| App Server → AgentCore | AWS 퍼블릭 API 호출 | App Server 에서 `bedrock-agentcore` API 로 나가는 아웃바운드. 프라이빗 서브넷이면 NAT 또는 VPC 엔드포인트 |
| AgentCore → App Server | Runtime 이 입력 조회·결과 저장·콜백 호출 | Runtime 의 subnet 이 App Server 에 닿아야 하고, App Server security group 인바운드에 Runtime SG 허용 |

두 번째가 빠지면 접수는 되는데 처리가 전부 실패한다. 202 는 나가고 그 뒤 입력 조회가
timeout 으로 죽는 모양이 된다.

## 5. 수동 배포와 검증

**5-1. 배포한다.** Actions → **Deploy Production** → `Run workflow`.
이번에는 `deploy` job 이 끝까지 간다. 요약에 새 Runtime 버전과 **직전 서비스 버전**
(롤백 대상)이 표로 남는다.

**5-2. 호출 계약을 확인한다.** #89 로 진입점 하나가 두 작업을 받는다. 두 종류를 모두
쏴 본다.

```bash
# 타임라인
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "<agentRuntimeArn>" \
  --qualifier prod \
  --payload '{"requestType":"TIMELINE","payload":{"taskId":"...","taskToken":"...","dailyRecordId":42,"window":{"startAt":"...","endAt":"..."}}}' \
  --region ap-northeast-2 \
  response.json

# User Memory
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "<agentRuntimeArn>" \
  --qualifier prod \
  --payload '{"requestType":"USER_MEMORY_UPDATE","payload":{"taskId":"...","taskToken":"...","userMemory":null,"dailyTimelines":[]}}' \
  --region ap-northeast-2 \
  response.json
```

> `invoke-agent-runtime` 의 인자 이름과 payload 전달 방식(`--payload` 인라인 / `fileb://`)은
> AWS CLI 버전에 따라 다를 수 있다. 실행 전에 `aws bedrock-agentcore invoke-agent-runtime help`
> 로 확인한다. 요청 **본문의 모양**은 CLI 와 무관하게 위와 같다.

기대 응답은 둘 다 `{"taskId": "...", "status": "PROCESSING"}` 이다. 202 는 완료가 아니라
접수이며, 최종 결과는 타임라인이면 완료 콜백, User Memory 면 결과 저장 호출로 나간다.
형식 상세는 [AI 서버 API 명세 3.1](ai-server-api.md#31-agentcore-호출-계약)에 있다.

**5-3. 실제 작업 하나를 끝까지 돌린다.** App Server 에서 타임라인 작업을 하나 넣고
입력 조회 → 결과 저장 → 콜백이 다 도는지 본다. 여기서 막히면 4-3 의 두 번째 방향을
먼저 의심한다.

**5-4. 로그를 확인한다.** Runtime 실행 역할에 CloudWatch Logs 권한이 있으므로 로그가
남는다. 컨테이너는 `LOG_FORMAT=json` 으로 구조화 로그를 낸다.

## 6. 롤백 리허설

장애가 났을 때 처음 해보면 늦다. 배포가 성공한 상태에서 한 번 돌려 본다.

**6-1. 버전 목록만 확인한다.** Actions → **Rollback Production** → `Run workflow` 를
**버전을 비워 두고** 실행하면 현재 서비스 버전과 사용 가능한 버전을 요약에 출력하고 끝난다.

**6-2. 되돌린다.** 버전을 지정해 다시 실행한다. 이미지를 다시 빌드하지 않는다 — 각
Runtime 버전이 그때 배포된 ECR 이미지를 물고 있어서 엔드포인트가 가리키는 버전만 바뀐다.

**6-3. 다시 최신으로 올린다.** 리허설이므로 원위치시킨다.

없는 버전이나 이미 서비스 중인 버전을 지정하면 전환하지 않고 실패한다. 배포와 롤백은
같은 concurrency 그룹이라 동시에 돌지 않는다.

## 7. 이 매뉴얼이 끝나면

- AgentCore 로 두 작업을 모두 접수·처리할 수 있다.
- 배포·롤백 이력이 Runtime 버전과 엔드포인트로 남는다.
- `dev` push 는 EC2(개발)로 계속 나간다. 이 경로는 그대로 유지된다.

**아직 자동 배포는 돌지 않는다.** GitHub 쪽 승격 경계가 남아 있다 —
[Production 배포 가이드](deploy-production.md) 의 `main` 브랜치 생성(§3.1),
ruleset(§3.2), Environment 승인자·배포 브랜치(§3.3), production ECR·IAM(§4).
둘 다 끝나야 `dev → main` merge 가 production 배포로 이어진다.

## 8. 자주 막히는 곳

| 증상 | 원인과 조치 |
|---|---|
| 워크플로가 `저장소 변수가 비어 있다` 로 멈춤 | 3장을 안 했다. 부트스트랩 중(2-1)이라면 정상 동작이다 |
| `AccessDenied` (`iam:PassRole`) | 배포 역할에 `PassRuntimeRole` 문이 빠졌다(1장) |
| Runtime 이 `CREATE_FAILED` / `UPDATE_FAILED` | `failureReason` 을 먼저 본다. 이미지 아키텍처(arm64), 포트(8080), `/ping` 응답, 실행 역할의 ECR pull 권한 순으로 확인한다 |
| App Server 가 `AccessDeniedException` | 4-2 의 호출 권한이 없다. control plane 권한이 있어도 data plane 호출은 따로다 |
| 202 는 오는데 처리가 전부 실패 | Runtime → App Server 경로다(4-3 두 번째 행). `networkMode` 가 `PUBLIC` 이거나 security group 인바운드가 없다 |
| 배포 후 `BEDROCK_MODEL` 등이 사라짐 | `UpdateAgentRuntime` 을 CLI 로 직접 부르며 `--environment-variables` 를 뺐을 때 생긴다. 워크플로는 기존 값을 읽어 보존한다 |
| 백그라운드 처리가 중간에 끊김 | uvicorn worker 를 늘렸는지 본다. in-flight 카운터가 프로세스 로컬이라 단일 worker 여야 한다 |
| `Invalid choice: 'bedrock-agentcore-control'` | 로컬 AWS CLI v2 가 오래됐다. 워크플로는 자동으로 갱신한다 |
