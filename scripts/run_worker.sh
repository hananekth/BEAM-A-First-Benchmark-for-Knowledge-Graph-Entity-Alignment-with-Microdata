#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="$ROOT_DIR/.run/worker.pid"
LOG_FILE="$ROOT_DIR/logs/worker.log"
MAX_JOBS="${MAX_CONCURRENT_JOBS:-2}"
POLL_INTERVAL="${JOB_POLL_INTERVAL:-1}"

mkdir -p .run logs

is_pid_running() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  if [[ -n "$OLD_PID" ]] && is_pid_running "$OLD_PID"; then
    echo "[OK] worker already running (pid=$OLD_PID)"
    exit 0
  fi
fi

PY_BIN="python"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PY_BIN="$ROOT_DIR/.venv/bin/python"
fi

nohup env MAX_CONCURRENT_JOBS="$MAX_JOBS" JOB_POLL_INTERVAL="$POLL_INTERVAL" "$PY_BIN" -m worker.run > "$LOG_FILE" 2>&1 &
NEW_PID="$!"
echo "$NEW_PID" > "$PID_FILE"

sleep 1
if is_pid_running "$NEW_PID"; then
  echo "[OK] worker started (pid=$NEW_PID) MAX_CONCURRENT_JOBS=$MAX_JOBS"
else
  echo "[ERR] worker failed to start; see $LOG_FILE" >&2
  exit 1
fi
