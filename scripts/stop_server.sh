#!/usr/bin/env bash
set -euo pipefail

pkill -f "python -m worker.run" || true
pkill -f "streamlit run app.py" || true

echo "[OK] stopped worker + streamlit"
