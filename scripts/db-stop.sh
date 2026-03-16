#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${ROOT_DIR}/.local/postgres/data"
PG_BIN="/usr/lib/postgresql/16/bin"

if [[ ! -f "${DATA_DIR}/PG_VERSION" ]]; then
  echo "No local Postgres data directory found at ${DATA_DIR}"
  exit 0
fi

"${PG_BIN}/pg_ctl" -D "${DATA_DIR}" stop || true
echo "PostgreSQL stopped (or was not running)."

