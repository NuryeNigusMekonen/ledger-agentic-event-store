#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.kafka.yml"
PURGE_DATA="${1:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but not installed." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker-compose)
else
  echo "Docker Compose is required but not installed." >&2
  exit 1
fi

if [[ "${PURGE_DATA}" == "--purge" ]]; then
  "${DOCKER_COMPOSE[@]}" -f "${COMPOSE_FILE}" down --volumes
  echo "Kafka stack stopped and Docker volumes removed."
  exit 0
fi

"${DOCKER_COMPOSE[@]}" -f "${COMPOSE_FILE}" down
echo "Kafka stack stopped."
