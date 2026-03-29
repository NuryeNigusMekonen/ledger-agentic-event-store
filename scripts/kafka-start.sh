#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.kafka.yml"

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

"${DOCKER_COMPOSE[@]}" -f "${COMPOSE_FILE}" up -d

cat <<EOF
Kafka stack is running with Docker.
- Broker: localhost:9092
- Kafka UI: http://localhost:8085
- Outbox relay container: ledger-outbox-relay

Use these env values for relay and API:
OUTBOX_PUBLISHER=kafka
KAFKA_BOOTSTRAP_SERVERS=localhost:9092

If your PostgreSQL password is not postgres/postgres, set:
OUTBOX_RELAY_DATABASE_URL=postgresql://<user>:<password>@host.docker.internal:55432/<db>
EOF
