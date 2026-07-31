#!/usr/bin/env bash

# GitHub Actions가 SSM Run Command로 EC2에 전달하는 배포 스크립트.
# EC2에는 컨테이너가 둘이다.
#
#   laimory-ai        애플리케이션. 배포마다 새 이미지로 교체한다.
#   laimory-filebeat  운영 로그 수집기. 앱 stdout을 읽어 Elasticsearch로 보낸다(#47).
#
# Filebeat는 앱과 생명주기가 분리돼 있다. 이미 정상 동작 중이면 배포가 건드리지
# 않는다 — 앱 교체 중에 수집기까지 내리면 그 사이 로그를 잃는다.
#
# 애플리케이션 비밀값과 Elasticsearch 접속정보는 GitHub를 거치지 않고 EC2의
# runtime.env / filebeat.env에 보관한다.

set -euo pipefail

readonly IMAGE_URI="${1:?사용법: deploy-ec2.sh IMAGE_URI AWS_REGION ECR_REGISTRY}"
readonly AWS_REGION="${2:?사용법: deploy-ec2.sh IMAGE_URI AWS_REGION ECR_REGISTRY}"
readonly ECR_REGISTRY="${3:?사용법: deploy-ec2.sh IMAGE_URI AWS_REGION ECR_REGISTRY}"
readonly CONTAINER_NAME="laimory-ai"
readonly ENV_FILE="${LAIMORY_ENV_FILE:-/opt/laimory-ai/runtime.env}"
readonly HOST_PORT="${LAIMORY_HOST_PORT:-8080}"
readonly IDLE_WAIT_ATTEMPTS=120
readonly READY_WAIT_ATTEMPTS=60

# --- Filebeat ----------------------------------------------------------------
readonly FILEBEAT_CONTAINER_NAME="laimory-filebeat"
readonly FILEBEAT_CONFIG="${LAIMORY_FILEBEAT_CONFIG:-/opt/laimory-ai/filebeat.yml}"
readonly FILEBEAT_ENV_FILE="${LAIMORY_FILEBEAT_ENV_FILE:-/opt/laimory-ai/filebeat.env}"
# registry(진행 위치)를 호스트에 남긴다. 이게 없으면 Filebeat가 재시작할 때마다
# 로그를 처음부터 다시 읽어 Elasticsearch에 중복이 쌓인다.
readonly FILEBEAT_DATA_DIR="${LAIMORY_FILEBEAT_DATA_DIR:-/opt/laimory-ai/filebeat-data}"
readonly FILEBEAT_CONFIG_LABEL="com.laimory.filebeat.config-sha256"
readonly FILEBEAT_READY_WAIT_ATTEMPTS=10

# json-file 드라이버의 기본값은 무제한이라 t3.micro 디스크가 조용히 찬다.
readonly LOG_OPTS=(--log-opt max-size=20m --log-opt max-file=3)

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "필수 명령이 없습니다: $1" >&2
    exit 1
  fi
}

container_exists() {
  docker inspect "${1:-$CONTAINER_NAME}" >/dev/null 2>&1
}

container_running() {
  [ "$(docker inspect --format '{{.State.Running}}' "${1:-$CONTAINER_NAME}" 2>/dev/null || true)" = "true" ]
}

ping_status() {
  docker exec "$CONTAINER_NAME" python -c \
    "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=5))['status'])" \
    2>/dev/null
}

start_container() {
  local image_uri="$1"
  # 배포한 이미지 태그를 그대로 버전으로 넘긴다. 태그에 git sha 가 들어 있어 Langfuse
  # release 와 관측 agentVersion 으로 배포본을 특정할 수 있다. 넘기지 않으면 pyproject
  # 버전(0.1.0)이 그대로 남아 어느 배포본의 trace 인지 구분되지 않는다(이슈 #48).
  # `-e` 는 `--env-file` 보다 우선하므로 runtime.env 에 값이 있어도 배포본 기준이 이긴다.
  local image_tag="${image_uri##*:}"
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -e "AGENT_VERSION=${image_tag}" \
    -p "${HOST_PORT}:8080" \
    "${LOG_OPTS[@]}" \
    "$image_uri"
}

# --- Filebeat 컨테이너 --------------------------------------------------------

filebeat_skip() {
  # 로그 수집 실패는 서비스 가용성 문제가 아니다. 배포를 멈추지 않고 상태만 알린다.
  echo "Filebeat: $1" >&2
  echo "FILEBEAT_STATUS=$2"
}

start_filebeat() {
  local image="$1"
  local config_sha="$2"

  docker run -d \
    --name "$FILEBEAT_CONTAINER_NAME" \
    --restart unless-stopped \
    --user root \
    --env-file "$FILEBEAT_ENV_FILE" \
    --label "${FILEBEAT_CONFIG_LABEL}=${config_sha}" \
    -v "${FILEBEAT_CONFIG}:/usr/share/filebeat/filebeat.yml:ro" \
    -v "${FILEBEAT_DATA_DIR}:/usr/share/filebeat/data" \
    -v /var/lib/docker/containers:/var/lib/docker/containers:ro \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    "${LOG_OPTS[@]}" \
    "$image" \
    filebeat -e --strict.perms=false
}

wait_until_filebeat_running() {
  for _ in $(seq 1 "$FILEBEAT_READY_WAIT_ATTEMPTS"); do
    if container_running "$FILEBEAT_CONTAINER_NAME"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

ensure_filebeat() {
  if [ ! -f "$FILEBEAT_CONFIG" ]; then
    filebeat_skip "설정 파일이 없어 건너뜁니다: ${FILEBEAT_CONFIG}" "skipped-no-config"
    return 0
  fi
  if [ ! -f "$FILEBEAT_ENV_FILE" ]; then
    filebeat_skip "접속정보 파일이 없어 건너뜁니다: ${FILEBEAT_ENV_FILE}" "skipped-no-env"
    return 0
  fi

  # FILEBEAT_IMAGE 는 Elasticsearch 버전에 맞춰야 해서 EC2 에서 정한다.
  local image
  image="$(grep -E '^FILEBEAT_IMAGE=' "$FILEBEAT_ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
  if [ -z "$image" ]; then
    filebeat_skip "FILEBEAT_IMAGE 가 ${FILEBEAT_ENV_FILE} 에 없습니다." "skipped-no-image"
    return 0
  fi

  local config_sha
  config_sha="$(sha256sum "$FILEBEAT_CONFIG" | cut -d' ' -f1)"

  if container_running "$FILEBEAT_CONTAINER_NAME"; then
    local current_image current_sha
    current_image="$(docker inspect --format '{{.Config.Image}}' "$FILEBEAT_CONTAINER_NAME")"
    current_sha="$(docker inspect --format "{{index .Config.Labels \"${FILEBEAT_CONFIG_LABEL}\"}}" "$FILEBEAT_CONTAINER_NAME" 2>/dev/null || true)"
    if [ "$current_image" = "$image" ] && [ "$current_sha" = "$config_sha" ]; then
      # 앱만 교체한다. 수집기를 같이 내리면 그 사이의 로그를 잃는다.
      echo "Filebeat: 이미 최신 설정으로 실행 중이라 그대로 둡니다."
      echo "FILEBEAT_STATUS=running"
      return 0
    fi
    echo "Filebeat: 이미지 또는 설정이 바뀌어 재생성합니다."
  fi

  mkdir -p "$FILEBEAT_DATA_DIR"
  docker pull "$image" >/dev/null 2>&1 || true
  docker rm -f "$FILEBEAT_CONTAINER_NAME" >/dev/null 2>&1 || true

  if ! start_filebeat "$image" "$config_sha" >/dev/null; then
    filebeat_skip "기동에 실패했습니다(앱 배포는 계속합니다)." "failed"
    return 0
  fi
  if ! wait_until_filebeat_running; then
    docker logs --tail 50 "$FILEBEAT_CONTAINER_NAME" >&2 || true
    filebeat_skip "기동 직후 종료됐습니다(앱 배포는 계속합니다)." "failed"
    return 0
  fi

  echo "Filebeat 기동 완료: ${image}"
  echo "FILEBEAT_STATUS=started"
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

  # 앱을 교체하기 **전에** 수집기를 세운다. 교체 직후에 세우면 새 컨테이너의 기동
  # 로그부터 놓친다. 여기서 실패해도 배포는 계속한다.
  ensure_filebeat

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
