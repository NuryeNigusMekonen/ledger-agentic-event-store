#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${ROOT_DIR}/.local/postgres/data"
RUN_DIR="${ROOT_DIR}/.local/postgres/run"
LOG_DIR="${ROOT_DIR}/.local/postgres/log"
LOG_FILE="${LOG_DIR}/server.log"
PG_BIN="/usr/lib/postgresql/16/bin"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

HOST="${PGHOST:-localhost}"
PORT="${PGPORT:-55432}"
USER="${PGUSER:-postgres}"
TARGET_DB="${PGDATABASE:-ledger_event_store}"

mkdir -p "${DATA_DIR}" "${RUN_DIR}" "${LOG_DIR}"

if [[ ! -f "${DATA_DIR}/PG_VERSION" ]]; then
  "${PG_BIN}/initdb" -D "${DATA_DIR}" -U postgres > "${LOG_DIR}/initdb.log" 2>&1
fi

if ! "${PG_BIN}/pg_ctl" -D "${DATA_DIR}" status > /dev/null 2>&1; then
  "${PG_BIN}/pg_ctl" \
    -D "${DATA_DIR}" \
    -l "${LOG_FILE}" \
    -o "-c listen_addresses='localhost' -k ${RUN_DIR} -p ${PORT}" \
    start
else
  echo "PostgreSQL already running."
fi

if ! psql -h "${HOST}" -p "${PORT}" -U "${USER}" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${TARGET_DB}'" | grep -q 1; then
  createdb -h "${HOST}" -p "${PORT}" -U "${USER}" "${TARGET_DB}"
  echo "Created database: ${TARGET_DB}"
else
  echo "Database already exists: ${TARGET_DB}"
fi

echo "PostgreSQL ready on ${HOST}:${PORT}"
