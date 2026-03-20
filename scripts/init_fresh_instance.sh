#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p .run/db_backups

if [[ -f jobs.db ]]; then
  TS="$(date +%Y%m%d_%H%M%S)"
  cp jobs.db ".run/db_backups/jobs_${TS}.db"
  [[ -f jobs.db-shm ]] && cp jobs.db-shm ".run/db_backups/jobs_${TS}.db-shm" || true
  [[ -f jobs.db-wal ]] && cp jobs.db-wal ".run/db_backups/jobs_${TS}.db-wal" || true
  echo "[OK] backup created in .run/db_backups/jobs_${TS}.db*"
fi

PY_BIN="python"
[[ -x "$ROOT_DIR/.venv/bin/python" ]] && PY_BIN="$ROOT_DIR/.venv/bin/python"

"$PY_BIN" - <<'PY'
from beam import db

# Keep presets (hardcoded in app), keep wdc_classes cache, clear runtime history/jobs only.
db.init_db()
conn = db._connect()
with conn:
    conn.execute('DELETE FROM job_events')
    conn.execute('DELETE FROM subjobs')
    conn.execute('DELETE FROM jobs')
print('[OK] jobs/subjobs/events cleared; presets unaffected')
PY

mkdir -p .run logs Download data
rm -f .run/webapp.pid .run/worker.pid

echo "[OK] fresh instance init complete"
