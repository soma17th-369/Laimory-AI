"""EC2 배포 후 ECR 이미지 보존 정책 검증."""

import json

from scripts import prune_ecr_images


def _detail(digest: str, *tags: str) -> dict:
    detail = {"imageDigest": digest}
    if tags:
        detail["imageTags"] = list(tags)
    return detail


def test_previous_tag_accepts_only_same_repository_tagged_uri() -> None:
    assert (
        prune_ecr_images._previous_tag(
            "laimory-ai",
            "123.dkr.ecr.ap-northeast-2.amazonaws.com/laimory-ai:previous",
        )
        == "previous"
    )
    assert (
        prune_ecr_images._previous_tag(
            "laimory-ai",
            "123.dkr.ecr.ap-northeast-2.amazonaws.com/other:previous",
        )
        is None
    )
    assert prune_ecr_images._previous_tag("laimory-ai", "") == ""


def test_select_stale_digests_keeps_current_and_actual_previous() -> None:
    details = [
        _detail("sha256:current", "current"),
        _detail("sha256:previous", "previous"),
        _detail("sha256:failed-deploy", "failed"),
        _detail("sha256:untagged"),
    ]

    assert prune_ecr_images._select_stale_digests(
        details,
        current_tag="current",
        previous_tag="previous",
    ) == ["sha256:failed-deploy", "sha256:untagged"]


def test_main_deletes_stale_images_in_batches_of_100(
    monkeypatch,
    tmp_path,
) -> None:
    details = [
        _detail("sha256:current", "current"),
        _detail("sha256:previous", "previous"),
        *[_detail(f"sha256:old-{index}") for index in range(205)],
    ]
    delete_batches: list[list[dict[str, str]]] = []

    def fake_aws_json(*args):
        if args[1] == "describe-images":
            return {"imageDetails": details}
        assert args[1] == "batch-delete-image"
        image_ids = args[args.index("--image-ids") + 1]
        delete_batches.append(json.loads(image_ids))
        return {"imageIds": delete_batches[-1], "failures": []}

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(prune_ecr_images, "_aws_json", fake_aws_json)

    result = prune_ecr_images.main(
        [
            "laimory-ai",
            "current",
            "123.dkr.ecr.region.amazonaws.com/laimory-ai:previous",
        ]
    )

    assert result == 0
    assert [len(batch) for batch in delete_batches] == [100, 100, 5]
    assert output.read_text(encoding="utf-8") == "deleted-count=205\n"


def test_main_skips_without_deleting_when_previous_tag_is_missing(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_aws_json(*args):
        calls.append(args)
        return {"imageDetails": [_detail("sha256:current", "current")]}

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(prune_ecr_images, "_aws_json", fake_aws_json)

    result = prune_ecr_images.main(
        [
            "laimory-ai",
            "current",
            "123.dkr.ecr.region.amazonaws.com/laimory-ai:previous",
        ]
    )

    assert result == 0
    assert len(calls) == 1
    assert output.read_text(encoding="utf-8") == "deleted-count=0\n"


def test_main_fails_safely_when_current_tag_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        prune_ecr_images,
        "_aws_json",
        lambda *_args: {"imageDetails": [_detail("sha256:old", "old")]},
    )

    assert prune_ecr_images.main(["laimory-ai", "current"]) == 1
