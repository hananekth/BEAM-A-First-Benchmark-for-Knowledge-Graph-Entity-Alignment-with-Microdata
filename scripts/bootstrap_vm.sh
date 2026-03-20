#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

WITH_DEV=0
if [[ "${1:-}" == "--dev" ]]; then
  WITH_DEV=1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERR] python3 not found in PATH" >&2
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
echo "[INFO] python3 detected: ${PY_VER}"

if [[ ! -d .venv ]]; then
  echo "[INFO] creating virtualenv .venv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
if [[ "$WITH_DEV" -eq 1 ]]; then
  python -m pip install -r requirements-dev.txt
fi

mkdir -p .run logs Download data .run/db_backups

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "[INFO] created .env from .env.example"
fi

python - <<'PY'
from beam import db

db.init_db()
print('[OK] jobs.db initialized/migrated')
PY

echo "[OK] bootstrap complete"
echo "[NEXT] start services: bash scripts/run_webapp.sh && bash scripts/run_worker.sh"
