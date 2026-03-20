#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_DIR="$ROOT_DIR/.run"
WORKER_PID_FILE="$PID_DIR/worker.pid"
WEBAPP_PID_FILE="$PID_DIR/webapp.pid"

is_pid_running() {
  local pid="$1"
  if [[ -z "${pid}" ]]; then
    return 1
  fi
  if ! [[ "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  kill -0 "${pid}" >/dev/null 2>&1
}

kill_pid_file() {
  local file="$1"
  local name="$2"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$file" || true)"
  if [[ -n "$pid" ]] && is_pid_running "$pid"; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 0.2
    if is_pid_running "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    echo "[OK] stopped ${name} (pid=$pid)"
  fi
  rm -f "$file"
}

kill_pid_file "$WORKER_PID_FILE" "worker"
kill_pid_file "$WEBAPP_PID_FILE" "webapp"

# Fallback cleanup for manual launches.
pkill -f "python -m worker.run" >/dev/null 2>&1 || true
pkill -f "uvicorn webapp.main:app" >/dev/null 2>&1 || true

echo "[OK] stopped worker + webapp"
