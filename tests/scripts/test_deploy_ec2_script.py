"""EC2 배포 스크립트의 정적 계약을 검증한다."""

from pathlib import Path


DEPLOY_SCRIPT = Path(__file__).parents[2] / "scripts" / "deploy-ec2.sh"


def test_filebeat_strict_perms_uses_long_option() -> None:
    """Filebeat long option은 하이픈 두 개가 없으면 ``-s``로 잘못 해석된다."""

    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "filebeat -e --strict.perms=false" in script
    assert "filebeat -e -strict.perms=false" not in script
