"""v1 API 라우터 취합."""

from fastapi import APIRouter

from app.api.v1 import timeline, timeline_testing, user_memory

router = APIRouter(prefix="/v1")
router.include_router(timeline.router, prefix="/timeline", tags=["timeline"])
# 동기 테스트 경로(#102). `/timeline` 아래이므로 접수 라우터보다 **뒤에** 붙여도
# 경로가 겹치지 않는다(`""` 와 `/test`). 노출 제한은 라우터가 아니라 이 모듈의
# 의존성과 `include_in_schema` 가 한다.
router.include_router(
    timeline_testing.router, prefix="/timeline", tags=["timeline-test"]
)
router.include_router(
    user_memory.router, prefix="/user-memory", tags=["user-memory"]
)
