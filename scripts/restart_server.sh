#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[INFO] restarting worker + webapp..."

"$ROOT_DIR/scripts/stop_server.sh" || true
sleep 1
"$ROOT_DIR/scripts/run_server.sh"

sleep 1

worker_ok="no"
webapp_ok="no"
if pgrep -f "python -m worker.run" >/dev/null 2>&1; then
  worker_ok="yes"
fi
if pgrep -f "uvicorn webapp.main:app" >/dev/null 2>&1; then
  webapp_ok="yes"
fi

echo "[OK] restart complete"
echo "[INFO] worker running: ${worker_ok}"
echo "[INFO] webapp running: ${webapp_ok}"
