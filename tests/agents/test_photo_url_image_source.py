"""`photoUrl` 이미지 다운로드 계약 (이슈 #52).

검증하는 것은 네 갈래다.

1. **정상 경로** — allowlist 안의 https URL 에서 받은 bytes 와 MIME 이 `ImageInput` 이 된다.
2. **거부 경로** — HTTP 오류·timeout·형식·크기·빈 응답은 예외 없이 `None` 이고,
   그래서 타임라인이 실패하지 않는다.
3. **URL 정책(SSRF)** — https 아님·userinfo·allowlist 밖·redirect 는 **요청을 보내기 전에**
   또는 따라가기 전에 막힌다.
4. **유출 금지** — 실패 로그 어디에도 URL 값과 query 가 남지 않는다.
"""

import logging
from threading import Event
from time import perf_counter

import httpx
import pytest

from app.agents.events.photo.image_source import (
    NullPhotoImageSource,
    PhotoImageSource,
    PhotoUrlImageSource,
    default_photo_image_source,
    load_images,
)
from app.agents.events.photo import image_source as image_source_module
from app.core.llm import ImageInput
from app.schemas import PhotoItem
from tests.fixtures.requests import fixture_raw_id

ALLOWED = ("images.example.com",)
PHOTO_URL = "https://images.example.com/a.jpg?X-Amz-Signature=deadbeefcafe&X-Amz-Expires=900"
JPEG = b"\xff\xd8\xff" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 64


def photo(url: str | None = PHOTO_URL, raw_id: str = "photo-1") -> PhotoItem:
    return PhotoItem(
        rawId=fixture_raw_id(raw_id), takenAt="2026-07-31T17:47:00+09:00", photoUrl=url
    )


def source_returning(
    handler, *, max_image_bytes: int | None = None
) -> PhotoUrlImageSource:
    return PhotoUrlImageSource(
        allowed_host_suffixes=ALLOWED,
        max_image_bytes=max_image_bytes,
        transport=httpx.MockTransport(handler),
    )


def respond(content: bytes, *, status: int = 200, content_type: str | None = "image/jpeg"):
    headers = {"content-type": content_type} if content_type else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content, headers=headers)

    return handler


# --- 정상 경로 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "content_type", "expected_mime"),
    [
        (JPEG, "image/jpeg", "image/jpeg"),
        (PNG, "image/png", "image/png"),
        (WEBP, "image/webp", "image/webp"),
        # 파라미터가 붙어 와도 벗겨서 본다.
        (JPEG, "image/jpeg; charset=binary", "image/jpeg"),
    ],
)
def test_downloads_allowed_image(body, content_type, expected_mime):
    image = source_returning(respond(body, content_type=content_type)).load(photo())

    assert isinstance(image, ImageInput)
    assert image.data == body
    assert image.mime_type == expected_mime


@pytest.mark.parametrize(("body", "expected_mime"), [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")])
def test_sniffs_mime_when_header_missing(body, expected_mime):
    # Content-Type 이 없으면 매직 바이트로 판별한다.
    image = source_returning(respond(body, content_type=None)).load(photo())

    assert image is not None and image.mime_type == expected_mime


def test_accepts_exactly_max_bytes():
    # App Server 장당 상한과 같은 값이라 경계가 통과해야 한다. 초과일 때만 거부한다.
    body = b"\xff\xd8\xff" + b"0" * (1024 - 3)
    image = source_returning(respond(body), max_image_bytes=1024).load(photo())

    assert image is not None and len(image.data) == 1024


# --- 거부 경로 ---------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 400, 403, 404, 500, 503])
def test_non_200_returns_none(status):
    # 3xx 도 여기서 걸린다 — redirect 를 따라가지 않는다.
    assert source_returning(respond(JPEG, status=status)).load(photo()) is None


def test_timeout_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    assert source_returning(handler).load(photo()) is None


def test_connect_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert source_returning(handler).load(photo()) is None


@pytest.mark.parametrize("content_type", ["text/html", "application/xml", "image/gif"])
def test_unsupported_content_type_returns_none(content_type):
    # image/gif 는 App Server 가 업로드를 거절하는 형식이라 받지 않는다.
    body = b"<Error><Code>AccessDenied</Code></Error>"
    assert source_returning(respond(body, content_type=content_type)).load(photo()) is None


def test_html_error_body_with_jpeg_magic_is_still_rejected():
    # 헤더가 있는데 허용 목록 밖이면 본문 시그니처를 믿지 않는다.
    assert source_returning(respond(JPEG, content_type="text/html")).load(photo()) is None


def test_unknown_bytes_without_header_returns_none():
    assert source_returning(respond(b"not-an-image", content_type=None)).load(photo()) is None


def test_empty_body_returns_none():
    assert source_returning(respond(b"")).load(photo()) is None


def test_body_over_max_bytes_returns_none():
    body = b"\xff\xd8\xff" + b"0" * 2048
    assert source_returning(respond(body), max_image_bytes=1024).load(photo()) is None


def test_declared_content_length_over_max_returns_none():
    # 선언 크기만으로 먼저 끊는다(본문을 다 받지 않는다).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=JPEG,
            headers={"content-type": "image/jpeg", "content-length": "999999"},
        )

    assert source_returning(handler, max_image_bytes=1024).load(photo()) is None


# --- URL 정책(SSRF) ----------------------------------------------------


def _fail_if_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"요청을 보내면 안 됩니다: {request.url.host}")


@pytest.mark.parametrize(
    "url",
    [
        "http://images.example.com/a.jpg",  # https 아님
        "file:///etc/passwd",
        "ftp://images.example.com/a.jpg",
        "https://images.example.com@169.254.169.254/latest/meta-data",  # userinfo
        "https://evil.com/a.jpg",  # allowlist 밖
        "https://evil-images.example.com.attacker.net/a.jpg",  # suffix 흉내
        "https://169.254.169.254/latest/meta-data",  # 링크로컬 직접 지정
        "https://localhost/a.jpg",
    ],
)
def test_rejects_unsafe_url_without_request(url):
    assert source_returning(_fail_if_called).load(photo(url)) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1/a.jpg",
        "https://images.example.com:invalid/a.jpg",
    ],
)
def test_malformed_url_is_rejected_without_raising_or_request(url):
    assert source_returning(_fail_if_called).load(photo(url)) is None


def test_allows_subdomain_of_allowed_host():
    source = PhotoUrlImageSource(
        allowed_host_suffixes=("example.com",),
        transport=httpx.MockTransport(respond(JPEG)),
    )

    assert source.load(photo("https://images.example.com/a.jpg")) is not None


def test_empty_allowlist_blocks_everything():
    source = PhotoUrlImageSource(
        allowed_host_suffixes=(), transport=httpx.MockTransport(_fail_if_called)
    )

    assert source.load(photo()) is None


def test_missing_photo_url_returns_none_quietly(caplog):
    with caplog.at_level(logging.DEBUG):
        assert source_returning(_fail_if_called).load(photo(None)) is None

    # 이미지 URL 이 없는 사진은 실패가 아니다. 오류 로그를 남기지 않는다.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- 유출 금지 ---------------------------------------------------------


def test_failure_log_does_not_leak_url(caplog):
    with caplog.at_level(logging.DEBUG):
        source_returning(respond(b"", status=403)).load(photo())

    logged = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "X-Amz-Signature" not in logged
    assert "deadbeefcafe" not in logged
    assert "images.example.com" not in logged
    # 진단에 필요한 값은 남는다.
    assert "403" in logged


def test_transport_error_log_has_safe_type_without_url_or_raw_id(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"failed to connect to {request.url}",
            request=request,
        )

    target = photo(raw_id="private-photo-id")
    with caplog.at_level(logging.DEBUG):
        assert source_returning(handler).load(target) is None

    logged = "\n".join(
        record.getMessage() + str(record.__dict__) for record in caplog.records
    )
    assert "private-photo-id" not in logged
    assert "images.example.com" not in logged
    assert "deadbeefcafe" not in logged
    assert "ConnectError" in logged


# --- 배치 로딩 ---------------------------------------------------------


def test_load_images_preserves_order_and_counts():
    photos = [photo(raw_id=f"photo-{i}") for i in range(3)]
    outcome = load_images(source_returning(respond(JPEG)), photos, max_workers=2)

    assert outcome.requested == 3
    assert outcome.succeeded == 3
    assert outcome.failed == 0
    assert [image.data for image in outcome.images] == [JPEG] * 3
    assert outcome.total_bytes == len(JPEG) * 3


def test_load_images_marks_failures_as_none():
    photos = [photo(raw_id="photo-0"), photo(None, raw_id="photo-1")]
    outcome = load_images(source_returning(respond(JPEG)), photos)

    assert outcome.images[0] is not None
    assert outcome.images[1] is None
    assert outcome.succeeded == 1 and outcome.failed == 1


def test_load_images_applies_max_images():
    photos = [photo(raw_id=f"photo-{i}") for i in range(5)]
    outcome = load_images(source_returning(respond(JPEG)), photos, max_images=3)

    assert outcome.succeeded == 3
    assert outcome.skipped == 2
    # 앞 3장만 실린다. 뒤 2장은 fallback 으로 간다.
    assert [image is not None for image in outcome.images] == [True, True, True, False, False]


def test_load_images_applies_total_bytes_budget():
    photos = [photo(raw_id=f"photo-{i}") for i in range(4)]
    # 2장까지만 들어가는 총량으로 자른다.
    outcome = load_images(
        source_returning(respond(JPEG)), photos, max_total_bytes=len(JPEG) * 2
    )

    assert outcome.succeeded == 2
    assert outcome.skipped == 2
    assert outcome.total_bytes <= len(JPEG) * 2


def test_load_images_empty_input():
    outcome = load_images(NullPhotoImageSource(), [])

    assert outcome.images == [] and outcome.requested == 0


def test_load_images_returns_when_batch_budget_expires():
    release = Event()

    class BlockingSource(PhotoImageSource):
        def load(self, target: PhotoItem) -> ImageInput | None:
            release.wait(timeout=1)
            return ImageInput(data=JPEG, mime_type="image/jpeg")

    started = perf_counter()
    try:
        outcome = load_images(
            BlockingSource(),
            [photo(raw_id="slow-photo")],
            max_workers=1,
            budget_sec=0.01,
        )
    finally:
        release.set()

    assert perf_counter() - started < 0.2
    assert outcome.succeeded == 0
    assert outcome.skipped == 1


def test_outcome_metadata_has_no_content():
    photos = [photo(raw_id="photo-0")]
    metadata = load_images(source_returning(respond(JPEG)), photos).as_metadata()

    assert set(metadata) == {
        "requested",
        "succeeded",
        "failed",
        "skipped",
        "byteLength",
        "durationMs",
    }


# --- 기본 조립 ---------------------------------------------------------


def test_default_source_is_url_when_allowlist_set(monkeypatch):
    monkeypatch.setattr(
        image_source_module.settings,
        "photo_url_allowed_hosts",
        "images.example.com",
    )

    assert isinstance(default_photo_image_source(), PhotoUrlImageSource)


def test_default_source_is_null_without_allowlist(monkeypatch):
    # 설정을 깜빡한 배포에서 임의 URL 로 요청이 나가지 않도록 fail closed 다.
    monkeypatch.setattr(image_source_module.settings, "photo_url_allowed_hosts", "")

    assert isinstance(default_photo_image_source(), NullPhotoImageSource)
