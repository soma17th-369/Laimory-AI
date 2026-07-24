# syntax=docker/dockerfile:1

# 같은 Dockerfile 을 AgentCore(linux/arm64)와 EC2(linux/amd64)에서 함께 쓴다.
# 플랫폼은 Dockerfile 에 고정하지 않고 각 GitHub Actions 워크플로의 `platforms`가
# 결정한다. 그래야 t3 계열 EC2와 AgentCore 양쪽 이미지를 같은 소스로 만들 수 있다.

FROM python:3.14-slim AS builder

# uv 로 uv.lock 그대로 재현 설치한다. dev 의존성(pytest 등)은 이미지에 넣지 않는다.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 이 프로젝트는 uv virtual project(`source = { virtual = "." }`)라 앱 코드가 패키지로
# 설치되지 않는다. 그래서 의존성 레이어는 pyproject.toml/uv.lock 에만 의존하고,
# 앱 소스가 바뀌어도 의존성 재설치가 일어나지 않는다.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.14-slim AS runtime

# 로그는 버퍼링 없이 stdout 으로 흘려 CloudWatch 가 그대로 수집하게 한다.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# 컨테이너 기본값. AgentCore environmentVariables 또는 EC2의 env 파일로 덮어쓴다.
# 모델 id, DB 접속, 콜백 URL, 관측 설정 같은 환경별 값은 이미지에 굽지 않는다.
ENV APP_ENV=prod \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    LLM_PROVIDER=bedrock

WORKDIR /app

# root 로 돌리지 않는다. 홈 디렉터리도 만들지 않고 앱 파일에 쓰기 권한을 주지 않는다.
RUN groupadd --system --gid 10001 laimory \
    && useradd --system --uid 10001 --gid 10001 --no-create-home laimory

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
# config.py 가 agent_version 을 pyproject.toml 에서 읽는다. virtual project 라
# importlib.metadata 로는 버전을 못 찾으므로, 관측 문서의 agentVersion 이 "unknown"
# 이 되지 않도록 pyproject.toml 을 함께 넣는다.
COPY pyproject.toml ./

USER laimory

# 서비스 포트. AgentCore 계약과 EC2 직접 배포가 모두 8080을 쓴다.
EXPOSE 8080

# 로컬·EC2 `docker run` 검증용. AgentCore 는 이 지시자가 아니라 GET /ping 을 직접 부른다.
#
# curl 대신 파이썬을 쓰는 이유: slim 이미지에 curl 이 없고, 헬스체크 하나 때문에
# 운영 이미지에 HTTP 클라이언트를 깔면 공격 표면만 넓어진다.
# urllib 대신 raw socket 을 써서 최소 의존성으로 빠르게 검사한다. start-period는
# arm64 이미지를 x86 개발기에서 QEMU로 검증할 때의 느린 기동도 허용하도록 넉넉히 둔다.
HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=3 \
    CMD ["python", "-S", "-c", "import socket; s=socket.create_connection(('127.0.0.1',8080),5); s.sendall(b'GET /ping HTTP/1.0\\r\\n\\r\\n'); raise SystemExit(0 if b' 200 ' in s.recv(64) else 1)"]

# worker 를 늘리지 않는다. 진행 중 처리 카운터(app/core/inflight.py)는 프로세스
# 로컬이며, EC2 배포 스크립트도 /ping 상태를 보고 처리 중 교체를 피한다.
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8080"]
