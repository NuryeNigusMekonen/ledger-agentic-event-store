#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${ROOT_DIR}/.local/postgres/data"
RUN_DIR="${ROOT_DIR}/.local/postgres/run"
LOG_DIR="${ROOT_DIR}/.local/postgres/log"
LOG_FILE="${LOG_DIR}/server.log"
PG_BIN="/usr/lib/postgresql/16/bin"
PORT="55432"

mkdir -p "${DATA_DIR}" "${RUN_DIR}" "${LOG_DIR}"

if [[ ! -f "${DATA_DIR}/PG_VERSION" ]]; then
  "${PG_BIN}/initdb" -D "${DATA_DIR}" -U postgres > "${LOG_DIR}/initdb.log" 2>&1
fi

"${PG_BIN}/pg_ctl" \
  -D "${DATA_DIR}" \
  -l "${LOG_FILE}" \
  -o "-c listen_addresses='localhost' -k ${RUN_DIR} -p ${PORT}" \
  start

echo "PostgreSQL started on localhost:${PORT}"

