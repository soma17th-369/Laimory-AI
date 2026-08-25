from contextlib import asynccontextmanager
from time import perf_counter

import uvicorn
from fastapi import FastAPI

from app.api.agentcore import router as agentcore_router
from app.api.error_handlers import register_error_handlers
from app.api.request_logging import RequestLoggingMiddleware
from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import align_uvicorn_loggers
from app.core.operational_logging import OperationalEvent, emit_event
from app.core.secrets import prefetch_secrets


# uvicorn 은 앱을 import 하기 전에 자기 로거에 직접 핸들러를 붙인다. 그래서 여기 —
# import 시점 — 가 uvicorn 로그를 우리 포맷으로 끌어올 수 있는 가장 이른 자리다.
# 더 늦게 하면 "Started server process" 같은 기동 로그가 JSON 이 아닌 채로 나가고,
# Filebeat 는 그 줄을 구조화하지 못한다.
align_uvicorn_loggers()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # `uvicorn.run()` 처럼 앱을 먼저 import 하고 나서 로깅을 설정하는 경로도 있다.
    # 그때는 위 호출이 덮여 버리므로 여기서 한 번 더 맞춘다(멱등).
    align_uvicorn_loggers()
    # 시크릿 번들을 여기서 한 번 읽는다(#30). LLM 호출은 `asyncio.to_thread` 위 동기
    # 컨텍스트라 그 안에서 외부 조회를 하지 않는다. 조회 실패는 기동을 막지 않는다.
    prefetch_secrets()
    started = perf_counter()
    # 배포·재시작·비정상 종료의 기준선이다. 컨테이너가 언제 올라오고 언제 내려갔는지
    # 를 알아야 "요청이 끊긴 구간" 을 설명할 수 있다.
    emit_event(
        OperationalEvent.SERVER_STARTED,
        appEnv=settings.app_env,
        logFormat=settings.log_format,
    )
    try:
        yield
    finally:
        emit_event(
            OperationalEvent.SERVER_STOPPED,
            appEnv=settings.app_env,
            uptimeMs=round((perf_counter() - started) * 1000, 3),
        )


app = FastAPI(lifespan=lifespan)
# 요청 로그를 라우팅보다 바깥에서 남긴다. 404/405 처럼 라우터에 닿지 못한 요청도
# 같은 계약으로 기록된다.
app.add_middleware(RequestLoggingMiddleware)
# 오류 응답을 경로와 무관하게 {"errorCode": int, "error": str} 한 가지로 통일한다.
# 라우터보다 먼저 붙여, 라우팅 자체가 실패하는 404/405 도 같은 계약으로 나가게 한다.
register_error_handlers(app)
app.include_router(v1_router)
# AgentCore Runtime 컨테이너 계약(`/invocations`, `/ping`). 경로가 고정이라 prefix 없이 붙인다.
app.include_router(agentcore_router)


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
