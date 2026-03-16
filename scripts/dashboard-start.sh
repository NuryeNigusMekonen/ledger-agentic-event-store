#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/apps/web"

if [[ ! -d node_modules ]]; then
  echo "node_modules not found. Run: npm install"
  exit 1
fi

exec npm run dev
