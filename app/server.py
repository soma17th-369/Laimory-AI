import uvicorn
from fastapi import FastAPI

from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

app = FastAPI()
app.include_router(v1_router)

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
    uvicorn.run(app, host="127.0.0.1", port=8000)
