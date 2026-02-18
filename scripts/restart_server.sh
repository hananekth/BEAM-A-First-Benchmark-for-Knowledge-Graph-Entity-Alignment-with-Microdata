#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[INFO] restarting worker + webapp..."

"$ROOT_DIR/scripts/stop_server.sh" || true
sleep 1
"$ROOT_DIR/scripts/run_server.sh"

sleep 1

PID_DIR="$ROOT_DIR/.run"
WORKER_PID_FILE="$PID_DIR/worker.pid"
WEBAPP_PID_FILE="$PID_DIR/webapp.pid"
worker_ok="no"
webapp_ok="no"
if [[ -f "$WORKER_PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$WORKER_PID_FILE" || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
    worker_ok="yes (pid=$pid)"
  fi
fi
if [[ -f "$WEBAPP_PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$WEBAPP_PID_FILE" || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
    webapp_ok="yes (pid=$pid)"
  fi
fi

echo "[OK] restart complete"
echo "[INFO] worker running: ${worker_ok}"
echo "[INFO] webapp running: ${webapp_ok}"
