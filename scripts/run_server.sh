#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uvicorn >/dev/null 2>&1; then
  echo "[ERR] uvicorn not found. Run: pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p logs
mkdir -p .run

PORT=8501
HOST="${WEBAPP_HOST:-0.0.0.0}"
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

read_pid_file() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    return 1
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$file" || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  echo "$pid"
}

start_worker() {
  local pid=""
  if pid="$(read_pid_file "$WORKER_PID_FILE")" && is_pid_running "$pid"; then
    echo "[OK] worker already running (pid=$pid)"
    return 0
  fi
  rm -f "$WORKER_PID_FILE"
  nohup env MAX_CONCURRENT_JOBS="${MAX_CONCURRENT_JOBS:-8}" JOB_POLL_INTERVAL="${JOB_POLL_INTERVAL:-1}" python -m worker.run > logs/worker.log 2>&1 &
  local new_pid="$!"
  echo "$new_pid" > "$WORKER_PID_FILE"
  sleep 0.2
  if is_pid_running "$new_pid"; then
    echo "[OK] worker started (pid=$new_pid)"
    return 0
  fi
  echo "[ERR] failed to start worker; check logs/worker.log" >&2
  return 1
}

start_webapp() {
  local pid=""
  if pid="$(read_pid_file "$WEBAPP_PID_FILE")" && is_pid_running "$pid"; then
    echo "[OK] webapp already running (pid=$pid)"
    return 0
  fi
  rm -f "$WEBAPP_PID_FILE"
  nohup uvicorn webapp.main:app --host "$HOST" --port "$PORT" > logs/webapp.log 2>&1 &
  local new_pid="$!"
  echo "$new_pid" > "$WEBAPP_PID_FILE"
  sleep 0.4
  if is_pid_running "$new_pid"; then
    echo "[OK] webapp started (pid=$new_pid)"
    return 0
  fi
  echo "[ERR] failed to start webapp; check logs/webapp.log" >&2
  return 1
}

wait_webapp_ready() {
  local tries=20
  local url="http://127.0.0.1:${PORT}/"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

start_worker
start_webapp

if wait_webapp_ready; then
  echo "[OK] webapp is reachable on 127.0.0.1:${PORT}"
else
  echo "[WARN] webapp process started but health check failed on 127.0.0.1:${PORT}" >&2
fi

echo "[INFO] open http://<server-ip>:${PORT} (direct access) or via SSH tunnel"
