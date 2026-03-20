#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PORT="${WEBAPP_PORT:-8501}"
BASE_URL="http://127.0.0.1:${PORT}"

status_pid() {
  local file="$1"
  local name="$2"
  if [[ ! -f "$file" ]]; then
    echo "[WARN] $name pid file missing"
    return
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$file" || true)"
  if [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
    echo "[OK] $name running (pid=$pid)"
  else
    echo "[WARN] $name not running (stale pid file: $pid)"
  fi
}

status_pid "$ROOT_DIR/.run/webapp.pid" "webapp"
status_pid "$ROOT_DIR/.run/worker.pid" "worker"

if curl -fsS "$BASE_URL/" >/dev/null 2>&1; then
  echo "[OK] webapp HTTP reachable at $BASE_URL"
else
  echo "[WARN] webapp HTTP not reachable at $BASE_URL"
fi

python_bin="python"
[[ -x "$ROOT_DIR/.venv/bin/python" ]] && python_bin="$ROOT_DIR/.venv/bin/python"

"$python_bin" - <<'PY'
import sqlite3
from pathlib import Path

db = Path('jobs.db')
if not db.exists():
    print('[WARN] jobs.db not found')
else:
    with sqlite3.connect(db) as conn:
        jobs = conn.execute('select count(*) from jobs').fetchone()[0]
        running = conn.execute("select count(*) from jobs where status in ('running','queued')").fetchone()[0]
    print(f'[OK] jobs.db present | jobs={jobs} active={running}')
PY
