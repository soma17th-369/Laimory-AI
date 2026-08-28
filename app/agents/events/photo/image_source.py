"""사진 이미지 소스 (이슈 #52).

`VisionPhotoDescriber` 가 실제 사진 픽셀을 vision 모델에 넘기려면 이미지 bytes 가
필요하다. 운영에서는 App Server 가 PHOTO payload 에 실어 주는 `photoUrl`(S3 이미지
URL)에서 내려받는다.

- `PhotoUrlImageSource`: 운영 경로이자 **기본값**. `photo.photo_url` 을 그대로 내려받는다.
- `NullPhotoImageSource`: 항상 이미지 없음(None). 테스트에서 다운로드를 끄고 fallback
  경로만 볼 때 주입한다. 제품 코드가 기본값으로 고르는 일은 없다.

## URL 정책

호스트 allowlist 는 **없다**(#59). 예전에는 `PHOTO_URL_ALLOWED_HOSTS` 가 비어 있으면
아예 내려받지 않는 fail-closed 였는데, 그 설정이 실제 배포에 들어간 적이 없어 사진
vision 이 한 번도 돌지 못했다. `photoUrl` 은 App Server 가 주는 값이라 신뢰하고,
설정 없이 항상 요청한다.

남아 있는 것은 `.env` 와 무관한 **형식 검사**뿐이다. 요청을 보내기 전에 막는다.

1. 파싱되지 않는 URL (잘못 닫힌 IPv6 literal, 범위를 벗어난 port …)
2. `http`/`https` 가 아닌 scheme (`file://`, `gopher://` …)
3. userinfo(`user:pass@host`) — 실제 목적지를 호스트 이름 뒤에 숨기는 형태
4. 호스트가 없는 URL

전부 "이 URL 로는 이미지를 받을 수 없다" 가 자명한 경우다. 외부 입력 하나 때문에
Photo Agent 전체가 실패하지 않도록 예외 대신 거부 사유로 바꿔 fallback 으로 보낸다.

요청에서 redirect 는 따라가지 않는다(`follow_redirects=False`). presigned S3 는
redirect 를 쓰지 않으므로 정상 경로에 영향이 없고, 필요해지면 `http_status` 3xx 로
로그에 바로 드러난다.

## 로그에 남기지 않는 것

`photoUrl` 은 presigned URL 이면 query 에 서명 자격증명이 실린다. **URL 값·호스트·
query·이미지 본문·rawId 는 어떤 경로로도 기록하지 않는다.** 남기는 것은 실패 사유
라벨, HTTP 상태, 소요 시간, bytes 크기, MIME type 뿐이다.
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import report_error
from app.core.execution_context import ExecutionStage
from app.core.llm import ImageInput
from app.core.logging import get_logger
from app.core.operational_logging import emit_degraded
from app.schemas import PhotoItem

logger = get_logger(__name__)

#: 받아들일 이미지 MIME type. App Server 가 업로드를 허용하는 3종과 같다.
ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

#: 이미지를 받을 수 있는 scheme. `file://` 같은 로컬 접근을 막는 최소 형식 검사다.
_ALLOWED_SCHEMES = frozenset({"https", "http"})

#: Content-Type 헤더가 없을 때 본문 앞부분으로 형식을 판별하는 시그니처.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class PhotoImageSource(ABC):
    """사진 하나에 대한 실제 이미지 bytes 를 제공하는 인터페이스."""

    @abstractmethod
    def load(self, photo: PhotoItem) -> ImageInput | None:
        """사진의 이미지를 불러온다. 구하지 못하면 None(예외를 던지지 않는다)."""


class NullPhotoImageSource(PhotoImageSource):
    """항상 이미지 없음을 반환하는 소스 (테스트 주입용)."""

    def load(self, photo: PhotoItem) -> ImageInput | None:
        return None


def _normalized_mime(content_type: str | None, body: bytes) -> str | None:
    """응답에서 허용된 MIME type 을 뽑는다. 판별하지 못하면 None."""

    if content_type:
        # "image/jpeg; charset=binary" 처럼 파라미터가 붙어 온다.
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime in ALLOWED_IMAGE_MIME_TYPES:
            return mime
        if mime:
            # 헤더가 있는데 허용 목록 밖이면 본문을 믿지 않는다. text/html 은 대개
            # S3 의 오류 문서다.
            return None

    for signature, mime in _MAGIC_SIGNATURES:
        if body.startswith(signature):
            return mime
    # WebP 는 "RIFF"(4) + 크기(4) + "WEBP"(4) 형태라 별도로 본다.
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


class PhotoUrlImageSource(PhotoImageSource):
    """`photo.photo_url` 에서 이미지를 내려받는 운영 소스."""

    def __init__(
        self,
        *,
        timeout_sec: float | None = None,
        max_image_bytes: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout_sec = (
            timeout_sec
            if timeout_sec is not None
            else settings.photo_download_timeout_sec
        )
        self._max_image_bytes = (
            max_image_bytes
            if max_image_bytes is not None
            else settings.photo_max_image_bytes
        )
        # 전송 계층 주입 지점. 운영에서는 None(기본 전송)이고, 테스트가
        # `httpx.MockTransport` 를 꽂아 네트워크 없이 응답을 지정한다.
        self._transport = transport

    def load(self, photo: PhotoItem) -> ImageInput | None:
        url = (photo.photo_url or "").strip()
        if not url:
            # 이미지 URL 이 없는 사진은 실패가 아니다. 조용히 fallback 으로 보낸다.
            return None

        rejection = self._reject_url(url)
        if rejection is not None:
            self._report(rejection)
            return None

        started = perf_counter()
        try:
            return self._download(url, started)
        except httpx.HTTPError as exc:
            self._report(
                "transport_error",
                error_type=type(exc).__name__,
                duration_ms=(perf_counter() - started) * 1000,
            )
            return None

    # --- 내부 ----------------------------------------------------------

    def _reject_url(self, url: str) -> str | None:
        """요청을 보내기 전에 URL 형식을 확인한다. 통과하면 None.

        호스트는 보지 않는다(#59). 여기 남은 것은 "이 URL 로는 애초에 이미지를 받을
        수 없다" 가 자명한 경우뿐이다.
        """

        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            # urllib.parse는 port 속성에 접근할 때 숫자/범위를 검증한다.
            # HTTP 클라이언트까지 잘못된 URL을 넘기지 않도록 여기서 파싱을 끝낸다.
            parsed.port
        except ValueError:
            # 잘못 닫힌 IPv6 literal 등은 urlsplit/hostname 접근에서 ValueError가 난다.
            # 외부 입력 하나 때문에 Photo Agent 전체가 실패하지 않게 거부 사유로 바꾼다.
            return "url_invalid"
        if parsed.scheme not in _ALLOWED_SCHEMES:
            return "scheme_not_supported"
        if "@" in parsed.netloc:
            return "userinfo_present"

        if not host:
            return "host_missing"
        return None

    def _download(self, url: str, started: float) -> ImageInput | None:
        with httpx.Client(
            timeout=self._timeout_sec,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            with client.stream("GET", url) as response:
                duration_ms = (perf_counter() - started) * 1000

                if response.status_code != 200:
                    # 3xx 도 여기서 걸린다. redirect 를 따라가지 않으므로 allowlist
                    # 를 통과한 호스트가 내부 주소로 넘기는 경로가 막힌다.
                    self._report(
                        "http_status",
                        http_status=response.status_code,
                        duration_ms=duration_ms,
                    )
                    return None

                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit():
                    if int(declared) > self._max_image_bytes:
                        self._report(
                            "too_large",
                            byte_length=int(declared),
                            duration_ms=duration_ms,
                        )
                        return None

                body = self._read_capped(response)
                if body is None:
                    self._report(
                        "too_large",
                        duration_ms=(perf_counter() - started) * 1000,
                    )
                    return None

                duration_ms = (perf_counter() - started) * 1000
                if not body:
                    self._report("empty_body", duration_ms=duration_ms)
                    return None

                mime = _normalized_mime(response.headers.get("content-type"), body)
                if mime is None:
                    self._report(
                        "unsupported_media_type",
                        byte_length=len(body),
                        duration_ms=duration_ms,
                    )
                    return None

                logger.debug(
                    "사진 이미지 다운로드 완료: bytes=%d, mime=%s",
                    len(body),
                    mime,
                )
                return ImageInput(data=body, mime_type=mime)

    def _read_capped(self, response: httpx.Response) -> bytes | None:
        """상한까지만 읽는다. 상한을 넘기면 즉시 중단하고 None.

        `Content-Length` 를 안 주거나 거짓으로 주는 응답이 있어, 선언값만 믿지 않고
        실제 누적으로도 막는다.
        """

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_image_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _report(
        self,
        reason: str,
        *,
        error_type: str | None = None,
        http_status: int | None = None,
        byte_length: int | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """실패 하나를 코드와 함께 남긴다. URL·rawId·예외 원문은 넣지 않는다."""

        context: dict[str, object] = {"reason": reason}
        if error_type is not None:
            context["errorType"] = error_type
        if http_status is not None:
            context["httpStatus"] = http_status
        if byte_length is not None:
            context["byteLength"] = byte_length
        report_error(
            logger,
            ErrorCode.PHOTO_IMAGE_FETCH_FAILED,
            "사진 이미지 다운로드 실패",
            context=context,
            stage=ExecutionStage.EVENT_AGENT,
            agent="photo",
            duration_ms=duration_ms,
            # 사진마다 부른다. 몇 장을 잃었는지는 `load_images` 가 배치 끝에
            # `droppedCount` 로 한 번만 낸다.
            emit=False,
        )


@dataclass
class ImageLoadOutcome:
    """배치 다운로드 결과. Langfuse 관측과 describer 가 함께 쓴다.

    `images` 는 입력 `photos` 와 **같은 길이·같은 순서**다. 구하지 못한 자리는 None
    이라, 호출부가 인덱스로 짝을 맞출 수 있다.
    """

    images: list[ImageInput | None]
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    #: 장수·총량 상한에 걸려 아예 시도하지 않았거나 버린 수.
    skipped: int = 0
    total_bytes: int = 0
    duration_ms: float = 0.0

    def as_metadata(self) -> dict[str, object]:
        """관측에 실을 값. 본문·URL 은 들어가지 않는다."""

        return {
            "requested": self.requested,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "byteLength": self.total_bytes,
            "durationMs": round(self.duration_ms, 1),
        }


def load_images(
    source: PhotoImageSource,
    photos: list[PhotoItem],
    *,
    max_images: int | None = None,
    max_total_bytes: int | None = None,
    max_workers: int | None = None,
    budget_sec: float | None = None,
) -> ImageLoadOutcome:
    """사진 목록의 이미지를 병렬로 불러온다.

    순차로 받으면 사진 20장에 장당 timeout 이 겹칠 때 `PIPELINE_TIMEOUT_SEC` 를
    위협한다. 그래서 제한 병렬로 받되 세 가지 상한을 둔다.

    - **장수**(`max_images`): App Server 요청당 상한과 같다.
    - **총 bytes**(`max_total_bytes`): describer 가 배치 1회 vision 호출이라, 여기서
      받은 총량이 그대로 provider 요청 크기가 된다.
    - **시간**(`budget_sec`): 배치 전체 상한.

    상한에 걸린 사진은 `None` 이 되어 호출부의 메타데이터 fallback 으로 흘러간다.
    설명이 비지는 않는다.
    """

    max_images = max_images if max_images is not None else settings.photo_max_images
    max_total_bytes = (
        max_total_bytes
        if max_total_bytes is not None
        else settings.photo_max_total_image_bytes
    )
    max_workers = (
        max_workers if max_workers is not None else settings.photo_download_max_workers
    )
    budget_sec = (
        budget_sec if budget_sec is not None else settings.photo_download_budget_sec
    )

    outcome = ImageLoadOutcome(images=[None] * len(photos), requested=len(photos))
    if not photos:
        return outcome

    # 장수 상한을 넘는 뒷부분은 시도하지 않는다.
    targets = list(enumerate(photos))[:max_images]
    outcome.skipped = len(photos) - len(targets)
    if outcome.skipped:
        logger.debug(
            "사진 장수 상한으로 일부를 내려받지 않습니다: 전체=%d, 대상=%d",
            len(photos),
            len(targets),
        )

    started = perf_counter()
    pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(targets))))
    try:
        # 워커 스레드에서도 taskId/stage 가 로그에 붙도록 컨텍스트를 복사해 넘긴다.
        # ThreadPoolExecutor 는 contextvars 를 자동으로 옮기지 않는다.
        futures = {
            pool.submit(contextvars.copy_context().run, source.load, photo): index
            for index, photo in targets
        }
        done, pending = wait(futures.keys(), timeout=budget_sec)

        outcome.skipped += len(pending)
        if pending:
            logger.warning(
                "사진 이미지 다운로드가 시간 예산을 넘겨 %d장을 포기합니다: budgetSec=%.1f",
                len(pending),
                budget_sec,
            )

        # 총량 상한은 완료된 것 중 앞 순서부터 채운다. 인덱스 순으로 보면 어떤 사진이
        # 실리는지가 실행마다 흔들리지 않는다.
        for future in sorted(done, key=lambda item: futures[item]):
            index = futures[future]
            try:
                image = future.result()
            except Exception as exc:  # noqa: BLE001 - 개별 사진 실패는 흡수한다.
                report_error(
                    logger,
                    ErrorCode.PHOTO_IMAGE_FETCH_FAILED,
                    "사진 이미지 다운로드 중 예기치 못한 오류",
                    context={
                        "errorType": type(exc).__name__,
                        "reason": "unexpected",
                    },
                    stage=ExecutionStage.EVENT_AGENT,
                    agent="photo",
                    emit=False,
                )
                outcome.failed += 1
                continue

            if image is None:
                # 실패 사유는 소스가 이미 코드와 함께 남겼다.
                outcome.failed += 1
                continue

            if outcome.total_bytes + len(image.data) > max_total_bytes:
                outcome.skipped += 1
                continue

            outcome.images[index] = image
            outcome.total_bytes += len(image.data)
            outcome.succeeded += 1
    finally:
        # `wait=False` 로 닫아야 예산이 실제로 지켜진다. `with` 블록은 종료 시
        # `shutdown(wait=True)` 라 아직 도는 다운로드를 전부 기다리게 된다.
        # 아직 시작하지 않은 작업은 취소되고, 이미 도는 것은 자기 timeout(장당
        # `photo_download_timeout_sec`) 안에 끝나므로 뒤에 남지 않는다.
        pool.shutdown(wait=False, cancel_futures=True)

    outcome.duration_ms = (perf_counter() - started) * 1000
    if outcome.skipped or outcome.failed:
        logger.debug(
            "사진 이미지 다운로드 요약: 요청=%d, 성공=%d, 실패=%d, 생략=%d, bytes=%d",
            outcome.requested,
            outcome.succeeded,
            outcome.failed,
            outcome.skipped,
            outcome.total_bytes,
        )
        # 장마다 내지 않고 배치 하나에 한 건이다(#101). 사진 설명은 이미지 없이도
        # metadata 로 이어지므로 작업은 성공으로 끝난다 — 그래서 이 이벤트가 없으면
        # 사진 근거가 통째로 빠진 하루도 운영에서 정상으로 보인다.
        emit_degraded(
            ExecutionStage.EVENT_AGENT,
            agent="photo",
            error_code=int(ErrorCode.PHOTO_IMAGE_FETCH_FAILED),
            dropped_count=outcome.failed + outcome.skipped,
            duration_ms=outcome.duration_ms,
        )
    return outcome


def default_photo_image_source() -> PhotoImageSource:
    """기본 이미지 소스를 만든다. 언제나 `photoUrl` 다운로드다.

    설정으로 켜고 끄지 않는다(#59). 예전에는 `PHOTO_URL_ALLOWED_HOSTS` 가 있어야
    URL 소스를 만들고 없으면 Null 이었는데, 그 설정이 실제 배포에 들어간 적이 없어
    사진 vision 이 한 번도 돌지 못했다. 조건을 없애 켜져 있는 것이 기본이 되게 한다.
    """

    return PhotoUrlImageSource()
