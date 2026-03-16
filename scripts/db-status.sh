#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT_DIR}/.local/postgres/run"
PORT="55432"

pg_isready -h localhost -p "${PORT}" -U postgres || true
echo "Socket dir: ${RUN_DIR}"

