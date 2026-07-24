#!/usr/bin/env python3
"""성공한 EC2 배포의 현재·직전 이미지만 ECR에 남긴다.

실패한 배포에서 push된 이미지가 중간에 끼어도 롤백 대상을 잘못 고르지 않도록,
push 시각이 아니라 EC2 배포 스크립트가 반환한 실제 직전 이미지 태그를 보존한다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

_DELETE_BATCH_SIZE = 100


def _aws_json(*args: str) -> dict[str, Any]:
    """AWS CLI를 호출하고 JSON 객체를 반환한다."""

    result = subprocess.run(
        ["aws", *args, "--output", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"aws {' '.join(args)} 실패: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI 응답이 올바른 JSON이 아닙니다.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AWS CLI 응답이 JSON 객체가 아닙니다.")
    return payload


def _write_output(name: str, value: str | int) -> None:
    """GitHub Actions step output이 있을 때만 값을 기록한다."""

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def _previous_tag(repository: str, image_uri: str) -> str | None:
    """같은 ECR 저장소의 tagged URI에서 태그를 추출한다."""

    if not image_uri:
        return ""
    marker = f"/{repository}:"
    if marker not in image_uri:
        return None
    return image_uri.rsplit(":", maxsplit=1)[-1]


def _contains_tag(detail: dict[str, Any], tag: str) -> bool:
    tags = detail.get("imageTags") or []
    return isinstance(tags, list) and tag in tags


def _select_stale_digests(
    image_details: list[dict[str, Any]],
    *,
    current_tag: str,
    previous_tag: str,
) -> list[str]:
    """보존 태그가 없는 tagged/untagged image digest를 고른다."""

    stale: list[str] = []
    for detail in image_details:
        if _contains_tag(detail, current_tag):
            continue
        if previous_tag and _contains_tag(detail, previous_tag):
            continue
        digest = detail.get("imageDigest")
        if isinstance(digest, str) and digest:
            stale.append(digest)
    return stale


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _delete_digests(repository: str, digests: list[str]) -> int:
    """ECR API 제한에 맞춰 digest를 최대 100개씩 삭제한다."""

    deleted = 0
    for batch in _chunks(digests, _DELETE_BATCH_SIZE):
        image_ids = json.dumps(
            [{"imageDigest": digest} for digest in batch],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = _aws_json(
            "ecr",
            "batch-delete-image",
            "--repository-name",
            repository,
            "--image-ids",
            image_ids,
        )
        failures = response.get("failures") or []
        if failures:
            raise RuntimeError(
                "ECR 이미지 일부 삭제 실패: "
                + json.dumps(failures, ensure_ascii=False, separators=(",", ":"))
            )
        deleted += len(batch)
    return deleted


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ECR에서 현재 배포 이미지와 실제 직전 이미지만 보존합니다."
    )
    parser.add_argument("repository", help="ECR repository 이름")
    parser.add_argument("current_tag", help="현재 배포한 이미지 태그")
    parser.add_argument(
        "previous_image_uri",
        nargs="?",
        default="",
        help="배포 전 컨테이너가 사용하던 이미지 URI",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    previous_tag = _previous_tag(args.repository, args.previous_image_uri)
    if previous_tag is None:
        print(
            "::warning::직전 이미지가 대상 ECR 저장소의 tagged URI가 아니어서 "
            f"정리를 건너뜁니다: {args.previous_image_uri}"
        )
        _write_output("deleted-count", 0)
        return 0

    try:
        response = _aws_json(
            "ecr",
            "describe-images",
            "--repository-name",
            args.repository,
            "--filter",
            "tagStatus=ANY",
        )
        raw_details = response.get("imageDetails") or []
        image_details = [
            detail for detail in raw_details if isinstance(detail, dict)
        ]

        if not any(_contains_tag(detail, args.current_tag) for detail in image_details):
            raise RuntimeError(
                "현재 배포 이미지 태그를 ECR에서 찾을 수 없어 정리를 중단합니다: "
                f"{args.current_tag}"
            )

        if previous_tag and not any(
            _contains_tag(detail, previous_tag) for detail in image_details
        ):
            print(
                "::warning::직전 이미지 태그를 ECR에서 찾을 수 없어 정리를 "
                f"건너뜁니다: {previous_tag}"
            )
            _write_output("deleted-count", 0)
            return 0

        stale_digests = _select_stale_digests(
            image_details,
            current_tag=args.current_tag,
            previous_tag=previous_tag,
        )
        deleted = _delete_digests(args.repository, stale_digests)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    kept = args.current_tag + (f",{previous_tag}" if previous_tag else "")
    print(f"ECR 정리 완료: 보존 태그={kept}, 삭제={deleted}")
    _write_output("deleted-count", deleted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
