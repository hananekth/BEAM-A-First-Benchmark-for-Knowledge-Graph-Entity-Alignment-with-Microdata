#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uvicorn >/dev/null 2>&1; then
  echo "[ERR] uvicorn not found. Run: pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p logs

PORT=8501

if pgrep -f "python -m worker.run" >/dev/null 2>&1; then
  echo "[OK] worker already running"
else
  nohup python -m worker.run > logs/worker.log 2>&1 &
  echo "[OK] worker started"
fi

if pgrep -f "uvicorn webapp.main:app" >/dev/null 2>&1; then
  echo "[OK] webapp already running"
else
  nohup uvicorn webapp.main:app --host 0.0.0.0 --port 8501 > logs/webapp.log 2>&1 &
  echo "[OK] webapp started"
fi

echo "[INFO] open http://<server-ip>:${PORT} (direct access) or via SSH tunnel"
