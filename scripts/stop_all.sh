#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

stop_pid_file() {
  local pid_file="$1"
  local label="$2"
  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$pid_file" || true)"
  if [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 1
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    echo "[OK] stopped $label (pid=$pid)"
  fi
  rm -f "$pid_file"
}

stop_pid_file "$ROOT_DIR/.run/webapp.pid" "webapp"
stop_pid_file "$ROOT_DIR/.run/worker.pid" "worker"

pkill -f "uvicorn webapp.main:app" >/dev/null 2>&1 || true
pkill -f "python -m worker.run" >/dev/null 2>&1 || true

echo "[OK] all services stopped"
