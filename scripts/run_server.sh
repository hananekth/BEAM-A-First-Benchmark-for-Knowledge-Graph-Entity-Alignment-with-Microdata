#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v streamlit >/dev/null 2>&1; then
  echo "[ERR] streamlit not found. Run: pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p logs

if pgrep -f "python -m worker.run" >/dev/null 2>&1; then
  echo "[OK] worker already running"
else
  nohup python -m worker.run > logs/worker.log 2>&1 &
  echo "[OK] worker started"
fi

if pgrep -f "streamlit run app.py" >/dev/null 2>&1; then
  echo "[OK] streamlit already running"
else
  nohup streamlit run app.py --server.port 8501 --server.address 0.0.0.0 > logs/streamlit.log 2>&1 &
  echo "[OK] streamlit started"
fi

echo "[INFO] open http://localhost:8501 (or via SSH tunnel)"
