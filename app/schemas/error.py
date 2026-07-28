"""공통 오류 응답 계약.

이 서버가 실패를 밖으로 내보내는 형태는 하나다.

.. code-block:: json

    {"errorCode": 1901, "error": "타임라인 생성 중 오류가 발생했습니다."}

- ``errorCode``: 오류 종류를 식별하는 정수(:class:`~app.core.error_codes.ErrorCode`)
- ``error``: 그 오류를 설명하는 **비어 있지 않은** 문자열

클라이언트는 ``error`` 문자열을 파싱하지 않고 ``errorCode`` 정수로만 분기한다.
메시지 문구는 바뀔 수 있고 코드는 바뀌지 않는다.

``error`` 에 들어가는 문장은 :mod:`app.core.error_codes` 카탈로그가 정본이다.
원본 예외 메시지를 그대로 담지 않는다 — 내부 식별자·경로·구현 세부가 새어 나간다.
"""

from pydantic import Field, model_validator

from app.core.error_codes import RESERVED_CODES, ErrorCode, message_for
from app.schemas.common import CamelModel


class ErrorResponse(CamelModel):
    """API 오류 응답 본문.

    외부 계약 타입은 정수지만, 값과 메시지는 반드시 활성 카탈로그 항목이어야 한다.
    그래서 직접 생성하는 코드도 임의 코드·원본 예외 메시지를 응답에 넣을 수 없다.
    """

    error_code: int = Field(alias="errorCode")
    error: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog_entry(self) -> "ErrorResponse":
        try:
            code = ErrorCode(self.error_code)
        except ValueError as exc:
            raise ValueError("errorCode는 오류 코드 카탈로그에 있어야 합니다.") from exc
        if code in RESERVED_CODES:
            raise ValueError("예약된 errorCode는 응답에 사용할 수 없습니다.")
        if self.error != message_for(code):
            raise ValueError("error는 오류 코드 카탈로그의 안전 메시지여야 합니다.")
        return self

    @classmethod
    def of(cls, code: ErrorCode) -> "ErrorResponse":
        """활성 카탈로그 코드와 안전 메시지로 응답 본문을 만든다."""

        return cls(error_code=int(code), error=message_for(code))
