"""배포 workflow 의 정적 계약을 검증한다 (이슈 #90).

여기서 고정하는 것들은 **틀려도 CI 가 초록으로 통과하고, 운영에서만 드러난다.**

- production job 에 `environment` 가 빠지면 승인 게이트와 전용 자격증명이 통째로 없어진다.
- `PROD_ECR_REPOSITORY` 대신 `ECR_REPOSITORY` 를 쓰면 운영 이미지가 개발 저장소로 올라가
  다음 dev 배포의 ECR 정리에 지워진다. 값이 비어 있지 않아 필수 설정 확인도 통과한다.
- PR guard 의 job 이름을 바꾸면 ruleset 의 필수 check 이름과 어긋나 보호가 조용히 풀린다.

세 가지 다 사람이 눈으로 보기 어려운 자리라 여기서 형태를 고정한다.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"

DEPLOY_PRODUCTION = WORKFLOWS / "deploy-production.yml"
ROLLBACK_PRODUCTION = WORKFLOWS / "rollback-production.yml"
PR_MAIN_GUARD = WORKFLOWS / "pr-main-guard.yml"
DEPLOY_EC2 = WORKFLOWS / "deploy-ec2.yml"

#: ruleset 의 required status check `context` 와 **글자 그대로** 같아야 한다.
#: docs/github/main-ruleset.example.json 과 docs/deploy-production.md §3.2 참고.
REQUIRED_CHECK_NAME = "dev 브랜치에서 온 PR 인지 확인"

#: production 환경 이름. OIDC trust policy 의 `sub` 에도 이 값이 들어간다.
PRODUCTION_ENVIRONMENT = "production"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # YAML 1.1 은 따옴표 없는 `on` 을 boolean True 로 읽는다. 두 키를 모두 본다.
    return workflow.get("on") or workflow[True]


@pytest.fixture(scope="module")
def deploy_production() -> dict:
    return _load(DEPLOY_PRODUCTION)


@pytest.fixture(scope="module")
def rollback_production() -> dict:
    return _load(ROLLBACK_PRODUCTION)


@pytest.fixture(scope="module")
def pr_main_guard() -> dict:
    return _load(PR_MAIN_GUARD)


@pytest.fixture(scope="module")
def deploy_ec2() -> dict:
    return _load(DEPLOY_EC2)


# --- trigger 경계 -------------------------------------------------------------


def test_production_deploys_only_from_main_push(deploy_production) -> None:
    """PR 시점이 아니라 merge 된 뒤에만 배포한다."""

    triggers = _triggers(deploy_production)

    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    # pull_request 로 돌면 merge 되지 않은 코드가 운영에 나간다.
    assert "pull_request" not in triggers


def test_production_deploy_has_no_path_filter(deploy_production) -> None:
    """main HEAD 와 운영에 떠 있는 커밋이 어긋나지 않게 한다 (계획의 승인 전 결정)."""

    assert "paths" not in _triggers(deploy_production)["push"]


def test_ec2_deploys_only_from_dev_push(deploy_ec2) -> None:
    """개발 경로가 main 을 trigger 로 갖지 않는다."""

    triggers = _triggers(deploy_ec2)

    assert triggers["push"]["branches"] == ["dev"]


def test_pr_guard_runs_on_base_change(pr_main_guard) -> None:
    """`edited` 가 없으면 base 를 나중에 main 으로 바꿔 검사를 건너뛸 수 있다."""

    triggers = _triggers(pr_main_guard)

    assert triggers["pull_request"]["branches"] == ["main"]
    assert "edited" in triggers["pull_request"]["types"]
    assert "synchronize" in triggers["pull_request"]["types"]


# --- 승인 게이트와 자격증명 경계 -----------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    ["deploy_production", "rollback_production"],
)
def test_production_jobs_declare_the_environment(fixture_name, request) -> None:
    """AWS 를 만지는 job 은 전부 environment 아래 있어야 한다.

    environment 를 선언한 job 만 승인 게이트에 걸리고 production 자격증명을 받는다.
    """

    workflow = request.getfixturevalue(fixture_name)

    for name, job in workflow["jobs"].items():
        assert job.get("environment") == PRODUCTION_ENVIRONMENT, (
            f"{fixture_name} 의 job '{name}' 에 environment 가 없다"
        )


def test_ec2_workflow_has_no_environment(deploy_ec2) -> None:
    """개발 workflow 가 production 자격증명에 닿지 못하게 한다."""

    for job in deploy_ec2["jobs"].values():
        assert "environment" not in job


def test_production_uses_its_own_ecr_repository(deploy_production) -> None:
    """ECR 저장소 이름을 공유하면 등록 누락이 조용한 오배포가 된다.

    이름이 같으면 Environment 등록을 빠뜨렸을 때 오류 대신 저장소 값(laimory-ai)이
    쓰이고, 운영 이미지가 개발 저장소로 올라가 다음 dev 배포의 ECR 정리에 지워진다.

    배포 역할(AWS_DEPLOY_ROLE_ARN)은 dev 와 **공용**이라 여기서 가르지 않는다.
    자원 경계는 역할이 아니라 ECR 저장소와 Environment 승인 게이트가 만든다.
    """

    body = DEPLOY_PRODUCTION.read_text(encoding="utf-8")

    assert "vars.PROD_ECR_REPOSITORY" in body
    assert "vars.ECR_REPOSITORY" not in body
    assert "secrets.AWS_DEPLOY_ROLE_ARN" in body


def test_ec2_never_references_production_names() -> None:
    """반대 방향도 막는다. 배포 역할은 공용이라 목록에 없다."""

    body = DEPLOY_EC2.read_text(encoding="utf-8")

    for name in (
        "PROD_ECR_REPOSITORY",
        "AGENTCORE_RUNTIME_ID",
        "AGENTCORE_ENDPOINT_NAME",
    ):
        assert f"vars.{name}" not in body
        assert f"secrets.{name}" not in body


def test_pr_guard_needs_no_credentials(pr_main_guard) -> None:
    """fork PR 에서도 도는 워크플로다. 쓰기 권한과 OIDC 를 갖지 않는다."""

    assert pr_main_guard["permissions"] == {"contents": "read"}

    body = PR_MAIN_GUARD.read_text(encoding="utf-8")
    assert "secrets." not in body
    assert "configure-aws-credentials" not in body


# --- ruleset 과 맞물리는 이름 --------------------------------------------------


def test_required_check_name_is_pinned(pr_main_guard) -> None:
    """ruleset 의 required status check `context` 와 같은 문자열이어야 한다."""

    jobs = pr_main_guard["jobs"]

    assert len(jobs) == 1, "job 이 늘면 어느 것이 필수 check 인지 모호해진다"
    assert next(iter(jobs.values()))["name"] == REQUIRED_CHECK_NAME


def test_ruleset_example_matches_the_guard_job_name() -> None:
    """예시 payload 와 workflow 가 어긋나면 필수 check 가 영원히 대기한다."""

    import json

    payload = json.loads(
        (Path(__file__).parents[2] / "docs" / "github" / "main-ruleset.example.json")
        .read_text(encoding="utf-8")
    )

    rules = {rule["type"]: rule for rule in payload["rules"]}

    # main 직접 push 차단은 pull_request 규칙이 담당한다.
    assert "pull_request" in rules
    assert payload["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
    assert payload["enforcement"] == "active"

    contexts = [
        check["context"]
        for check in rules["required_status_checks"]["parameters"][
            "required_status_checks"
        ]
    ]
    assert REQUIRED_CHECK_NAME in contexts


# --- 이미지와 롤백 -------------------------------------------------------------


def test_production_image_has_no_moving_tag(deploy_production) -> None:
    """Runtime 버전과 이미지가 1:1 이어야 롤백이 성립한다."""

    build = next(
        step
        for step in deploy_production["jobs"]["deploy"]["steps"]
        if step.get("id") == "build"
    )
    tags = build["with"]["tags"]

    assert isinstance(tags, str), "태그가 여럿이면 이동 태그가 섞였을 수 있다"
    assert tags.strip() == "${{ steps.meta.outputs.image-uri }}"
    assert build["with"]["platforms"] == "linux/arm64"


def test_production_does_not_prune_ecr(deploy_production) -> None:
    """production 저장소는 정리하지 않는다.

    AgentCore 는 cold start 마다 image 를 pull 하므로, 버전이 살아 있는 동안 image 가
    ECR 에 남아 있어야 한다. 롤백 대상 버전도 마찬가지다.

    주석에는 왜 그런지가 적혀 있으므로 raw text 가 아니라 실행되는 명령만 본다.
    """

    for step in deploy_production["jobs"]["deploy"]["steps"]:
        assert "prune_ecr_images" not in step.get("run", "")
        assert "batch-delete-image" not in step.get("run", "")


def test_deploy_and_rollback_share_a_concurrency_group(
    deploy_production, rollback_production
) -> None:
    """배포와 롤백이 동시에 돌면 엔드포인트 상태가 꼬인다."""

    deploy_group = deploy_production["concurrency"]
    rollback_group = rollback_production["concurrency"]

    assert deploy_group["group"] == rollback_group["group"]
    # 진행 중인 배포를 취소하면 Runtime 이 UPDATING 에서 멈출 수 있다.
    assert deploy_group["cancel-in-progress"] is False
    assert rollback_group["cancel-in-progress"] is False


def test_production_records_rollback_target_before_switching(deploy_production) -> None:
    """전환 전에 직전 버전을 기록해야 자동 복구가 가능하다."""

    steps = deploy_production["jobs"]["deploy"]["steps"]
    step_ids = [step.get("id") for step in steps]

    assert step_ids.index("before") < step_ids.index("switch")

    recovery = next(
        step for step in steps if "자동 복구" in step.get("name", "")
    )
    assert "failure()" in recovery["if"]
    assert "steps.switch.outputs.switched" in recovery["if"]
