"""v1 API 라우터 취합."""

from fastapi import APIRouter

from app.api.v1 import timeline, user_memory

router = APIRouter(prefix="/v1")
router.include_router(timeline.router, prefix="/timeline", tags=["timeline"])
router.include_router(
    user_memory.router, prefix="/user-memory", tags=["user-memory"]
)
