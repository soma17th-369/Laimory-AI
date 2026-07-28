import uvicorn
from fastapi import FastAPI

from app.api.agentcore import router as agentcore_router
from app.api.error_handlers import register_error_handlers
from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

app = FastAPI()
# 오류 응답을 경로와 무관하게 {"errorCode": int, "error": str} 한 가지로 통일한다.
# 라우터보다 먼저 붙여, 라우팅 자체가 실패하는 404/405 도 같은 계약으로 나가게 한다.
register_error_handlers(app)
app.include_router(v1_router)
# AgentCore Runtime 컨테이너 계약(`/invocations`, `/ping`). 경로가 고정이라 prefix 없이 붙인다.
app.include_router(agentcore_router)

logger.info("서버 초기화 완료 (APP_ENV=%s)", settings.app_env)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug/env")
def debug_env():
    return {
        "APP_ENV": settings.app_env,
        "OPENAI_API_KEY_EXISTS": bool(settings.openai_api_key),
    }

if __name__ == "__main__":
    uvicorn.run(app, host=settings.server_host, port=settings.server_port)
