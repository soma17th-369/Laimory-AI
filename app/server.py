import uvicorn
from fastapi import FastAPI

from app.api.agentcore import router as agentcore_router
from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

app = FastAPI()
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
