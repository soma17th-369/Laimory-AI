"""사용자 메모리 스키마.

사용자가 확정한 자신의 기록을 바탕으로 새롭게 정의되는 비정형 JSON 이다.
key/value 는 AI 가 자동으로 분석해 채우므로 고정 스키마를 두지 않고 자유로운
구조를 허용한다. 타임라인 생성 시 **보조 context 로만** 사용한다.
"""

from pydantic import ConfigDict

from app.schemas.common import CamelModel


class UserMemory(CamelModel):
    """비정형 사용자 메모리 (보조 context).

    선언된 고정 필드 없이 임의의 key/value 를 허용한다.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")
