# Production 배포 가이드 (main → AgentCore Runtime)

> 기준일: 2026-08-25 (이슈 #90)
> 대상: `main` 을 production 의 정본으로 삼아 AgentCore Runtime 에 배포하는 경로

`dev → main` PR 이 merge 되면 GitHub Actions 가 arm64 이미지를 production 전용 ECR 에
올리고, AgentCore Runtime 새 버전을 만들어 엔드포인트를 전환한다. `dev` push 는 지금처럼
EC2 로 나간다([EC2 배포 가이드](deploy-ec2.md)).

AgentCore Runtime 자체를 처음 만드는 절차는 [AgentCore 전환 수동 작업 매뉴얼](agentcore-cutover-manual.md)
에 있다. 이 문서는 **그 위에 `main` 기반 승격·배포 경계를 얹는 방법**을 다룬다.

## 1. 배포 구조

```text
feat/#N → dev  (PR merge)
  → deploy-ec2.yml → EC2 (개발, amd64, laimory-ai)

dev → main  (PR merge)
  → deploy-production.yml
  → 승인 대기 (Environment production)
  → linux/arm64 빌드 → laimory-ai-prod 에 push
  → UpdateAgentRuntime        (새 Runtime 버전 생성)
  → Runtime READY 대기         ← AgentCore 가 GET /ping 으로 판정
  → UpdateAgentRuntimeEndpoint (엔드포인트 전환 = 실제 배포)
  → 엔드포인트 READY 대기      ← 같은 /ping 판정
  → 호출 스모크 (비치명, 요약에만 기록)
  → 실패 시 직전 서비스 버전으로 자동 복구
```

| 구분 | 개발 | 운영 |
|---|---|---|
| 브랜치 | `dev` | `main` |
| 워크플로 | `deploy-ec2.yml` | `deploy-production.yml` |
| 대상 | EC2 단일 컨테이너 | AgentCore Runtime |
| 아키텍처 | `linux/amd64` | `linux/arm64` |
| ECR 저장소 | `laimory-ai` | `laimory-ai-prod` |
| 배포 역할 | `AWS_DEPLOY_ROLE_ARN` (공용) | 같은 역할, `environment:production` 으로 assume |
| 승인 | 없음 | 필요 (required reviewers) |
| 롤백 | 배포 스크립트 자동 복구 | `rollback-production.yml` |

## 2. `main` 직접 push 를 막는 세 겹

`push: branches: [main]` 만으로는 직접 push 와 PR merge 를 구분할 수 없다. 그래서 GitHub
설정과 워크플로를 함께 쓴다.

| 겹 | 수단 | 막는 것 |
|---|---|---|
| 1 | ruleset 의 `pull_request` 규칙 | `main` 직접 push. PR 을 강제한다 |
| 2 | `pr-main-guard.yml` | `dev` 이외 브랜치(및 fork)에서 온 PR |
| 3 | ruleset 의 필수 check 지정 | 2번이 실패한 PR 의 merge |

**3번이 없으면 2번은 경고에 그친다.** 실패한 check 를 무시하고 merge 할 수 있다.

## 3. GitHub 설정

아래는 저장소 밖에서 사람이 하는 작업이다. 순서를 지킨다 — Environment 가 없으면 변수를
넣을 곳이 없고, Runtime 을 만들기 전에는 ID 를 알 수 없다.

### 3.1 `main` 브랜치 만들기

현재 저장소에는 `main` 이 없다(기본 브랜치는 `dev`). `dev` 를 그대로 올려 시작점을 만든다.

```bash
git fetch origin
git push origin origin/dev:refs/heads/main
```

먼저 만들어야 ruleset 과 Environment 의 대상이 생긴다. 오래된 `prod` 브랜치는 이 경로와
무관하며 그대로 둔다.

### 3.2 ruleset 적용

저장소의 [`docs/github/main-ruleset.example.json`](github/main-ruleset.example.json) 을
그대로 쓴다.

```bash
gh api -X POST repos/soma17th-369/Laimory-AI/rulesets \
  --input docs/github/main-ruleset.example.json
```

> **저장소의 JSON 을 고쳐도 GitHub 설정은 바뀌지 않는다.** 이 파일은 적용용 payload 일
> 뿐이다. 내용을 바꿨으면 `gh api -X PUT repos/.../rulesets/<id> --input ...` 으로 다시
> 적용해야 효력이 생긴다.

적용 결과를 확인한다.

```bash
gh api repos/soma17th-369/Laimory-AI/rulesets
```

필수 check 의 `context` 는 `.github/workflows/pr-main-guard.yml` 의 job `name` 과 **글자
그대로** 같아야 한다. 현재 값은 `dev 브랜치에서 온 PR 인지 확인` 이다. 워크플로의 job
이름을 바꾸면 필수 check 가 영영 통과되지 않거나(이름이 안 맞아 check 가 안 옴) 조용히
무력화된다. `tests/scripts/test_workflow_contracts.py` 가 이 이름을 고정한다.

긴급 상황에 관리자 우회가 필요하면 `bypass_actors` 에 항목을 넣는다. 기본값은 비어
있다(아무도 우회하지 못한다).

### 3.3 Environment `production` 만들기

```bash
gh api -X PUT repos/soma17th-369/Laimory-AI/environments/production
```

만든 뒤 **웹 콘솔에서 두 가지를 반드시 설정한다** (Settings → Environments → production).

| 설정 | 값 | 이유 |
|---|---|---|
| Required reviewers | 배포를 승인할 사람 | merge 실수가 곧바로 운영 반영으로 이어지지 않게 한다 |
| Deployment branches | `main` 만 (Selected branches) | **이게 없으면 OIDC 신뢰 조건이 뚫린다.** §4.2 참고 |

### 3.4 변수와 시크릿

**저장소 수준은 개발 전용이 된다.** 이미 등록돼 있고 손대지 않는다.

| 이름 | 종류 | 값 | 쓰는 곳 |
|---|---|---|---|
| `AWS_REGION` | Variable | `ap-northeast-2` | 공용 |
| `ECR_REPOSITORY` | Variable | `laimory-ai` | dev 전용 |
| `EC2_INSTANCE_ID` | Variable | 개발 EC2 의 `i-...` | dev 전용 |
| `AWS_DEPLOY_ROLE_ARN` | Secret | 배포 역할 ARN | **dev·production 공용**(§4.2) |

**Environment `production` 수준에 새로 등록한다.** 세 개뿐이고 전부 Variable 이다.

| 이름 | 종류 | 값 | 출처 |
|---|---|---|---|
| `PROD_ECR_REPOSITORY` | Variable | `laimory-ai-prod` | §4.1 에서 만든 이름 |
| `AGENTCORE_RUNTIME_ID` | Variable | `laimory_ai-XXXXXXXXXX` | `create-agent-runtime` 응답의 `agentRuntimeId` |
| `AGENTCORE_ENDPOINT_NAME` | Variable | `prod` | `create-agent-runtime-endpoint` 의 `--name` |

```bash
# 지금 바로 정할 수 있는 것
gh variable set PROD_ECR_REPOSITORY --env production --body "laimory-ai-prod"

# AgentCore Runtime·엔드포인트를 만든 뒤에야 값이 생기는 것 (§5)
gh variable set AGENTCORE_RUNTIME_ID --env production --body "laimory_ai-XXXXXXXXXX"
gh variable set AGENTCORE_ENDPOINT_NAME --env production --body "prod"
```

배포 역할 secret 은 새로 만들지 않는다. 저장소 수준 `AWS_DEPLOY_ROLE_ARN` 을 dev 와 함께
쓰며, 그 역할의 신뢰 정책과 권한만 넓히면 된다(§4.2).

#### ECR 저장소 이름만 가른 이유

Environment 값은 **같은 이름의 저장소 값을 덮어쓴다.** 문제는 등록을 빠뜨렸을 때다.
덮어쓸 값이 없으면 오류가 아니라 **저장소 값이 조용히 쓰인다.**

`ECR_REPOSITORY` 를 그대로 썼다면 운영 이미지가 `laimory-ai`(개발)로 올라가고, 다음 dev
배포의 ECR 정리에 지워진다. 값이 비어 있지 않아 워크플로의 「필수 설정 확인」도 통과한다.
그래서 `PROD_ECR_REPOSITORY` 로 갈라 등록 누락이 곧 실패가 되게 했다.

`AWS_DEPLOY_ROLE_ARN` 은 dev 와 **공용이라 가르지 않는다.** 값이 하나뿐이라 덮어쓸 일도,
빠뜨릴 일도 없다. 대신 역할이 권한 경계가 아니게 되므로 경계는 다른 데서 만든다(§4.2).

`AGENTCORE_RUNTIME_ID` 와 `AGENTCORE_ENDPOINT_NAME` 은 저장소 수준에 짝이 없어 접두어를
붙이지 않았다. **저장소 수준에 같은 이름을 만들지 않는다.** 만드는 순간 같은 함정이 생긴다.

## 4. AWS 설정

### 4.1 production 전용 ECR 저장소

```bash
aws ecr create-repository \
  --repository-name laimory-ai-prod \
  --image-scanning-configuration scanOnPush=true \
  --region ap-northeast-2
```

**개발과 저장소를 나누는 것이 선택이 아닌 이유**가 있다. `scripts/prune_ecr_images.py` 는
저장소 전체를 훑어 EC2 배포의 현재·직전 태그가 **없는** 이미지를 아키텍처 구분 없이
지운다. `deploy-ec2.yml` 이 dev 배포 성공마다 이를 실행하므로, 한 저장소를 공유하면
dev merge 한 번에 운영 arm64 이미지가 사라진다.

**`laimory-ai-prod` 에는 lifecycle policy 를 걸지 않는다.** AgentCore 는 서버리스라
콜드스타트마다 이미지를 받는다. EC2 처럼 "pull 해 둔 이미지가 호스트에 남아 있다" 를
가정할 수 없고, 롤백 대상 버전이 물고 있는 이미지도 살아 있어야 한다. 정책이 걸려 있지
않은지 확인한다.

```bash
aws ecr get-lifecycle-policy --repository-name laimory-ai-prod --region ap-northeast-2
# LifecyclePolicyNotFoundException 이 나오면 정상이다.
```

용량이 문제가 되면 자동 삭제 대신, 어떤 Runtime 버전도 참조하지 않는 태그를
`list-agent-runtime-versions` 로 확인한 뒤 수동으로 지운다.

### 4.2 배포 역할 — 개발과 공용

**새 역할을 만들지 않는다.** 기존 `laimory-ai-github-deploy` 하나를 dev·production 이
함께 쓰고, GitHub 에는 저장소 수준 secret `AWS_DEPLOY_ROLE_ARN` 하나만 둔다.

대신 그 역할의 신뢰 정책과 권한을 넓혀야 한다. 지금은 `dev` 브랜치 조건만 있어
production job 이 역할을 맡지 못한다.

#### 신뢰 정책에 `environment:production` 을 더한다

`sub` 값이 둘이므로 `StringLike` 배열로 쓴다. `dev` push 는
`ref:refs/heads/dev`, production job 은 `environment:production` 으로 온다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::392900063927:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:soma17th-369/Laimory-AI:ref:refs/heads/dev",
            "repo:soma17th-369/Laimory-AI:environment:production"
          ]
        }
      }
    }
  ]
}
```

> **역할이 권한 경계가 아니다.** 하나를 공유하기로 했으므로, dev 경로에서 발급된 토큰도
> AgentCore 를 부를 권한을 갖는다. 경계는 다른 데서 만들어진다.
>
> | 무엇이 | 무엇을 막나 |
> |---|---|
> | ECR 저장소 분리 | dev 배포의 ECR 정리가 운영 이미지를 지우는 것 |
> | Environment 승인 게이트 | 승인 없는 production 배포 |
> | Deployment branches = `main` | `main` 이외 브랜치의 production 배포 |
> | 워크플로별 실행 브랜치 가드 | `deploy-ec2.yml` 을 `main` 에서, 롤백을 `main` 밖에서 돌리는 것 |
>
> `environment:production` 이라는 `sub` 값 자체에는 브랜치가 없다. **§3.3 의
> "Deployment branches = main" 설정이 브랜치 방어의 전부다.** 그것을 빼면 아무 브랜치에서나
> `environment: production` 을 선언한 job 이 이 역할을 가져갈 수 있다.
>
> 권한을 더 좁히고 싶으면 역할을 dev·production 으로 나누고 production 값을 Environment
> secret 으로 옮기면 된다. 그때는 이름을 `AWS_PROD_DEPLOY_ROLE_ARN` 처럼 갈라야 한다 —
> 이름이 같으면 등록을 빠뜨렸을 때 오류 대신 dev 역할이 조용히 쓰인다.

#### 권한 정책은 두 경로의 합집합이다

기존 EC2·ECR 권한에 아래를 더한다. ECR 은 저장소 두 개를 모두 넣는다.

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
      "Resource": [
        "arn:aws:ecr:ap-northeast-2:392900063927:repository/laimory-ai",
        "arn:aws:ecr:ap-northeast-2:392900063927:repository/laimory-ai-prod"
      ]
    },
    {
      "Sid": "AgentCoreDeploy",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:UpdateAgentRuntime",
        "bedrock-agentcore:ListAgentRuntimeVersions",
        "bedrock-agentcore:GetAgentRuntimeEndpoint",
        "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
        "bedrock-agentcore:InvokeAgentRuntime"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassRuntimeRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::392900063927:role/laimory-ai-agentcore-runtime",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
        }
      }
    }
  ]
}
```

EC2 배포용 `ssm:SendCommand`·`ssm:GetCommandInvocation`·`ecr:BatchDeleteImage` 문은 기존
정책에 있는 그대로 둔다([EC2 배포 가이드 §4](deploy-ec2.md)).

`iam:PassRole` 이 없으면 `UpdateAgentRuntime` 이 `AccessDenied` 로 떨어진다. Runtime 실행
역할 ARN 을 인자로 받기 때문이다. `InvokeAgentRuntime` 은 배포 후 호출 스모크에만 쓴다.
`AgentCoreDeploy` 의 `Resource` 는 Runtime 을 만든 뒤 실제 ARN 으로 좁히는 것을 권장한다.

**이 역할은 배포용이다.** 컨테이너가 Bedrock 을 부르거나 시크릿을 읽을 때 쓰는 것은
Runtime 실행 역할(`laimory-ai-agentcore-runtime`)이며 별개다.

### 4.3 Runtime 실행 역할도 production 저장소를 봐야 한다

배포 역할과 **별개**다. `laimory-ai-agentcore-runtime` 은 컨테이너를 띄울 때 이미지를
pull 하는 역할이고, 저장소를 가른 뒤로 그 대상이 `laimory-ai-prod` 다. 개발 저장소만
허용돼 있으면 Runtime 생성부터 실패한다.

```text
Access denied while validating ECR URI '...'. The execution role requires
permissions for ecr:GetAuthorizationToken, ecr:BatchGetImage, and
ecr:GetDownloadUrlForLayer operations.
```

AWS 콘솔이 만들어 주는 기본 실행 역할에는 이미 두 문장이 있다. **고칠 것은 한 곳뿐이다.**

```json
{
  "Sid": "EcrImageAccess",
  "Effect": "Allow",
  "Action": [
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer"
  ],
  "Resource": "arn:aws:ecr:ap-northeast-2:392900063927:repository/laimory-ai-prod"
},
{
  "Sid": "EcrTokenAccess",
  "Effect": "Allow",
  "Action": "ecr:GetAuthorizationToken",
  "Resource": "*"
}
```

`EcrImageAccess` 의 `Resource` 를 `laimory-ai` 에서 `laimory-ai-prod` 로 바꾸면 끝난다.
AgentCore 는 production 저장소에서만 pull 하므로 둘 다 둘 필요는 없다(두려면 배열로 쓴다).

`EcrTokenAccess` 는 이미 맞게 돼 있다. `ecr:GetAuthorizationToken` 은 **저장소 단위로 좁힐
수 없어** 반드시 별도 문장에서 `"*"` 다. 한 문장에 뭉치고 ARN 을 넣으면 인증 자체가 막힌다.

필요한 액션은 오류 메시지가 말한 셋뿐이다. `ecr:BatchCheckLayerAvailability` 는 push 쪽
액션이라 실행 역할에는 넣지 않아도 된다.

> **기본 역할을 통째로 덮어쓰지 않는다.** 콘솔이 만든 실행 역할에는 CloudWatch Logs 외에
> X-Ray(`xray:PutTraceSegments` 등), 메트릭(`cloudwatch:PutMetricData`,
> `namespace: bedrock-agentcore` 조건), `logs:PutResourcePolicy` 가 함께 들어 있다.
> ECR 문장 하나만 고치고 나머지는 그대로 둔다.

**ECR 저장소를 가르면 역할이 둘 바뀐다** — 배포 역할(§4.2)과 이 실행 역할이다. 하나만
고치면 배포는 되는데 Runtime 이 안 뜨거나, 그 반대가 된다.

### 4.4 ECR 정리 권한은 개발 저장소에만 둔다

`ecr:BatchDeleteImage` 의 `Resource` 를 `laimory-ai` 하나로 유지한다. 역할을 공유해도
`laimory-ai-prod` 에는 삭제 권한이 없어야, 정리 스크립트가 실수로 운영 저장소를 가리켜도
IAM 이 막는다.

## 5. AgentCore Runtime 과 엔드포인트

[AgentCore 전환 수동 작업 매뉴얼](agentcore-cutover-manual.md) 2장을 따른다. 이미지를 올릴
저장소가 `laimory-ai-prod` 라는 점만 다르다.

### 부트스트랩 순서 — 닭과 달걀

`AGENTCORE_RUNTIME_ID` 와 `AGENTCORE_ENDPOINT_NAME` 은 **Runtime 과 엔드포인트를 만들어야
생기는 값**이고, Runtime 은 **ECR 에 이미지가 있어야 만들 수 있다.** 그래서 순서가 정해져
있다.

```text
1. Deploy Production 을 main 에서 수동 실행
   → 빌드·ECR push 까지 끝나고 「AgentCore 설정 확인」에서 의도적으로 멈춘다
   → 요약과 로그에서 이미지 태그를 적어 둔다
2. 그 이미지로 Runtime 을 만든다        → agentRuntimeId 를 받는다
3. 전용 엔드포인트를 만든다(--name prod) → 이름은 직접 정한다
4. 두 값을 Environment production 에 등록한다
5. Deploy Production 을 다시 실행한다   → 이번에는 끝까지 간다
```

1번에서 **워크플로가 빨간불로 끝나는 것이 정상이다.** 「빌드 설정 확인」은 통과하고
빌드가 끝난 뒤 「AgentCore 설정 확인」이 막는다. 이미지는 이미 ECR 에 올라가 있다.

> 워크플로의 스텝 순서가 이 부트스트랩을 만든다. AgentCore 값 검사를 빌드 앞으로 옮기면
> 첫 실행이 이미지를 만들기도 전에 죽어 고리가 끊기지 않는다.
> `tests/scripts/test_workflow_contracts.py` 가 이 순서를 고정한다.

> 엔드포인트는 반드시 **전용 엔드포인트**(예: `prod`)로 만든다. `DEFAULT` 는 최신 버전을
> 따라가므로 롤백 지점으로 쓸 수 없다.

## 6. 배포

`dev → main` PR 을 merge 하면 시작된다. PR 을 열거나 고치는 시점에는 배포되지 않는다
(`push` 이벤트라 그렇다).

1. `dev` 에서 검증을 마친다.
2. `dev → main` PR 을 연다. `dev 브랜치에서 온 PR 인지 확인` check 가 통과해야 merge 된다.
3. merge 하면 **Deploy Production** 이 승인 대기 상태로 뜬다.
4. 승인자가 Actions 화면에서 승인하면 빌드·배포가 진행된다.
5. 요약에서 커밋 SHA, 이미지 태그, digest, 새 Runtime 버전, **직전 서비스 버전**을 확인한다.

path 필터를 두지 않았다. 문서만 바뀐 merge 도 배포가 돌아 `main` HEAD 와 운영에 떠 있는
커밋이 어긋나지 않는다. 불필요한 배포는 승인 단계에서 거절하면 된다.

### ECR push 는 배포가 아니다

AgentCore 는 ECR 을 감시하지 않는다. 이미지가 올라가도 저절로 반영되는 일은 없다.

```text
docker push                  → 저장소에 이미지가 생김. 아무 일도 일어나지 않는다
UpdateAgentRuntime           → containerUri 를 바꾼 새 버전(1, 2, 3 ...) 생성
UpdateAgentRuntimeEndpoint   → 엔드포인트가 가리키는 버전 전환  ← 이것이 배포다
```

"지금 무엇이 떠 있는가" 의 정본은 ECR 이 아니라 `엔드포인트 → Runtime 버전 →
containerUri → 이미지` 사슬이다. 롤백에 재빌드가 없는 이유도 여기 있다.

그래서 **이동 태그를 만들지 않는다.** 버전 N 과 이미지가 1:1 로 고정돼야 롤백이 성립하는데,
`latest` 나 `dev` 같은 태그를 `containerUri` 에 쓰면 서로 다른 버전이 같은 문자열을 가리켜
되돌려도 같은 이미지가 뜰 수 있다. 태그 형식은 다음과 같다.

```text
prod-sha-<커밋12자리>-arm64-run-<GitHub run id>-<attempt>
```

첫 배포 뒤 버전별로 실제 무엇이 박혔는지 한 번 확인해 둔다.

```bash
aws bedrock-agentcore-control list-agent-runtime-versions \
  --agent-runtime-id "$AGENTCORE_RUNTIME_ID" --region ap-northeast-2 \
  --query 'agentRuntimes[].[agentRuntimeVersion,agentRuntimeArtifact]'
```

### 배포 후 health check

치명 게이트는 **Runtime READY** 와 **엔드포인트 READY** 두 개다. AgentCore 가 컨테이너의
`GET /ping` 을 확인해야 READY 로 올리므로, 이 둘이 곧 `/ping` 검사다.

그 뒤의 호출 스모크는 `requestType` 에 알 수 없는 값을 넣어 응답이 오는지만 본다.
**배포를 실패시키지 않고 요약에만 남긴다** — `invoke-agent-runtime` 의 인자 형태가 CLI
버전마다 다르고 아직 실운영으로 검증하지 않아, 오탐이 정상 배포를 되돌리는 쪽이 더
위험하기 때문이다. 실제 운영에서 한 번 확인한 뒤 치명으로 올린다.

> 스모크 payload 의 `requestType` 은 반드시 **알 수 없는 값**이어야 한다. `app/api/agentcore.py`
> 가 `requestType` 키가 없는 body 를 TIMELINE 으로 감싸므로, 빈 body 를 보내면 진짜
> 타임라인 작업이 접수된다.

### 자동 복구

엔드포인트를 새 버전으로 이미 넘긴 뒤에 실패하면, 워크플로가 직전 서비스 버전으로 되돌리고
job 을 실패시킨다. 되돌리는 대상은 방금 전까지 실제로 서비스하던 버전이라 이미 검증돼 있다.

엔드포인트 전환 **전에** 실패하면 되돌릴 것이 없다. 서비스는 옛 버전 그대로다.

## 7. 롤백

Actions → **Rollback Production** → `Run workflow`. **`main` 브랜치에서 실행한다.**

1. **버전을 비워 두고** 실행하면 현재 서비스 버전과 사용 가능한 버전 목록만 요약에 찍고 끝난다.
2. 목록에서 고른 버전을 넣고 다시 실행한다.

이미지를 다시 빌드하지 않는다. 없는 버전이나 이미 서비스 중인 버전을 넣으면 전환하지 않고
실패한다. 배포와 같은 concurrency 그룹이라 동시에 돌지 않는다.

장애 때 처음 해보면 늦다. 첫 배포가 성공한 뒤 한 번 리허설한다(되돌렸다가 다시 최신으로).

## 8. 설정이 끝났는지 확인

```bash
# main 이 있는가
git ls-remote --heads origin main

# ruleset 이 걸렸는가
gh api repos/soma17th-369/Laimory-AI/rulesets

# Environment 와 변수·시크릿이 있는가 (값은 나오지 않는다)
gh api repos/soma17th-369/Laimory-AI/environments/production
gh variable list --env production

# production ECR 저장소가 있고 lifecycle policy 가 없는가
aws ecr describe-repositories --repository-names laimory-ai-prod --region ap-northeast-2
aws ecr get-lifecycle-policy --repository-name laimory-ai-prod --region ap-northeast-2
```

웹 콘솔에서만 확인되는 것이 둘 있다. **Required reviewers** 와 **Deployment branches =
main** 이다. 둘 다 Settings → Environments → production 에 있다.

## 9. 자주 막히는 곳

| 증상 | 원인과 조치 |
|---|---|
| merge 했는데 배포가 안 뜸 | Environment 승인 대기 중이다. Actions 화면의 `Review deployments` 를 본다 |
| `Environment 'production' 의 변수/시크릿이 비어 있다` | §3.4 를 안 했거나 저장소 수준에 넣었다. `--env production` 을 확인한다 |
| 운영 이미지가 `laimory-ai` 로 올라감 | `PROD_ECR_REPOSITORY` 미등록이다. 저장소 값이 조용히 쓰였다 |
| dev 배포가 갑자기 `AccessDenied` | 공용 역할의 trust policy 를 고치며 `ref:refs/heads/dev` 항목을 지웠다(§4.2) |
| `AccessDenied` (`sts:AssumeRoleWithWebIdentity`) | 공용 역할의 trust policy 에 `environment:production` 항목을 더했는지 본다(§4.2). `ref:refs/heads/main` 이 아니다 |
| `AccessDenied` (`iam:PassRole`) | §4.2 의 `PassRuntimeRole` 문이 빠졌다 |
| 아무 브랜치에서나 production 이 배포됨 | Environment 의 Deployment branches 가 `main` 으로 제한되지 않았다(§3.3) |
| `dev` 아닌 브랜치에서 온 PR 이 merge 됨 | check 는 실패했는데 ruleset 의 필수 check 지정이 없다(§3.2) |
| 필수 check 가 영원히 대기 중 | ruleset 의 `context` 문자열과 job `name` 이 다르다 |
| 롤백하려는 버전의 이미지가 없음 | `laimory-ai-prod` 에 lifecycle policy 가 걸렸거나 수동 삭제됐다(§4.1) |
| Runtime 이 `UPDATE_FAILED` | `failureReason` 부터 본다. arm64, 8080, `/ping`, 실행 역할의 ECR pull 권한 순으로 확인한다 |
| Runtime 생성이 `Access denied while validating ECR URI` | 실행 역할에 `laimory-ai-prod` pull 권한이 없다(§4.3). 배포 역할과 다른 역할이다 |
| 배포 후 환경변수가 사라짐 | CLI 로 `UpdateAgentRuntime` 을 직접 부르며 `--environment-variables` 를 뺐을 때 생긴다. 워크플로는 기존 값을 읽어 보존한다 |
