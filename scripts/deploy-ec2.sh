#!/usr/bin/env bash

# GitHub Actions가 SSM Run Command로 EC2에 전달하는 단일 컨테이너 배포 스크립트.
# 애플리케이션 비밀값은 GitHub를 거치지 않고 EC2의 runtime.env에 보관한다.

set -euo pipefail

readonly IMAGE_URI="${1:?사용법: deploy-ec2.sh IMAGE_URI AWS_REGION ECR_REGISTRY}"
readonly AWS_REGION="${2:?사용법: deploy-ec2.sh IMAGE_URI AWS_REGION ECR_REGISTRY}"
readonly ECR_REGISTRY="${3:?사용법: deploy-ec2.sh IMAGE_URI AWS_REGION ECR_REGISTRY}"
readonly CONTAINER_NAME="laimory-ai"
readonly ENV_FILE="${LAIMORY_ENV_FILE:-/opt/laimory-ai/runtime.env}"
readonly HOST_PORT="${LAIMORY_HOST_PORT:-8080}"
readonly IDLE_WAIT_ATTEMPTS=120
readonly READY_WAIT_ATTEMPTS=60

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "필수 명령이 없습니다: $1" >&2
    exit 1
  fi
}

container_exists() {
  docker inspect "$CONTAINER_NAME" >/dev/null 2>&1
}

container_running() {
  [ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" = "true" ]
}

ping_status() {
  docker exec "$CONTAINER_NAME" python -c \
    "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=5))['status'])" \
    2>/dev/null
}

start_container() {
  local image_uri="$1"
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -p "${HOST_PORT}:8080" \
    "$image_uri"
}

wait_until_idle() {
  if ! container_running; then
    return
  fi

  for _ in $(seq 1 "$IDLE_WAIT_ATTEMPTS"); do
    status="$(ping_status || true)"
    case "$status" in
      Healthy)
        echo "기존 컨테이너가 유휴 상태입니다."
        return
        ;;
      HealthyBusy)
        echo "기존 컨테이너가 처리 중입니다. 10초 뒤 다시 확인합니다."
        sleep 10
        ;;
      *)
        echo "기존 컨테이너의 /ping을 확인할 수 없어 교체를 중단합니다." >&2
        exit 1
        ;;
    esac
  done

  echo "기존 컨테이너가 20분 안에 유휴 상태가 되지 않았습니다." >&2
  exit 1
}

wait_until_ready() {
  for _ in $(seq 1 "$READY_WAIT_ATTEMPTS"); do
    status="$(ping_status || true)"
    case "$status" in
      Healthy|HealthyBusy)
        echo "새 컨테이너가 준비됐습니다: ${status}"
        return 0
        ;;
    esac
    sleep 5
  done
  return 1
}

rollback() {
  local previous_image="$1"

  echo "새 컨테이너 기동에 실패했습니다. 직전 이미지로 복구합니다." >&2
  docker logs --tail 200 "$CONTAINER_NAME" >&2 || true
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

  if [ -n "$previous_image" ]; then
    start_container "$previous_image" >/dev/null
    if wait_until_ready; then
      echo "직전 이미지 복구 완료: ${previous_image}" >&2
    else
      echo "직전 이미지도 정상 기동하지 못했습니다: ${previous_image}" >&2
    fi
  else
    echo "최초 배포라 복구할 직전 이미지가 없습니다." >&2
  fi
  exit 1
}

main() {
  require_command aws
  require_command docker

  case "$(uname -m)" in
    x86_64|amd64) ;;
    *)
      echo "현재 EC2 아키텍처는 amd64 이미지와 맞지 않습니다: $(uname -m)" >&2
      exit 1
      ;;
  esac

  if [ ! -f "$ENV_FILE" ]; then
    echo "운영 환경변수 파일이 없습니다: ${ENV_FILE}" >&2
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "Docker 데몬이 실행 중이 아니거나 접근할 수 없습니다." >&2
    exit 1
  fi

  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY"
  docker pull "$IMAGE_URI"
  docker logout "$ECR_REGISTRY" >/dev/null 2>&1 || true

  wait_until_idle

  previous_image=""
  if container_exists; then
    previous_image="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME")"
    docker rm -f "$CONTAINER_NAME" >/dev/null
  fi
  # GitHub Actions가 배포 성공 후 ECR 정리 시 실제 롤백 대상 이미지를 보존하는 데 쓴다.
  # 이미지 URI에는 인증정보가 없으며, 파싱 가능한 고정 접두어로 한 줄만 출력한다.
  echo "PREVIOUS_IMAGE_URI=${previous_image}"

  if ! start_container "$IMAGE_URI" >/dev/null; then
    rollback "$previous_image"
  fi
  if ! wait_until_ready; then
    rollback "$previous_image"
  fi

  echo "EC2 배포 완료: ${IMAGE_URI}"
}

main "$@"
