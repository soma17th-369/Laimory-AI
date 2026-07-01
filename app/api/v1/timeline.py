"""타임라인 초안 생성 엔드포인트."""

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, status

router = APIRouter()


class TimelineDraftRequest(BaseModel):
    """타임라인 초안 생성 요청 placeholder."""

    sources: list[dict] = Field(min_length=1)


class TimelineDraftResponse(BaseModel):
    """타임라인 초안 생성 응답 placeholder."""

    events: list[dict] = Field(default_factory=list)


@router.post("", response_model=TimelineDraftResponse, status_code=status.HTTP_200_OK)
def create_timeline_draft(request: TimelineDraftRequest) -> TimelineDraftResponse:
    """입력 소스로부터 타임라인 초안을 생성한다.

    실제 AI 파이프라인은 `app.pipeline.timeline_pipeline` 구현 후 이곳에 연결한다.
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "message": "타임라인 생성 파이프라인이 아직 구현되지 않았습니다.",
            "source_count": len(request.sources),
        },
    )
