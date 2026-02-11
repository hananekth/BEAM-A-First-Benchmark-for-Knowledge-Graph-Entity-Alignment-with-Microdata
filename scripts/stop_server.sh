#!/usr/bin/env bash
set -euo pipefail

pkill -f "python -m worker.run" || true
pkill -f "uvicorn webapp.main:app" || true

echo "[OK] stopped worker + webapp"
