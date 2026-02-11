#!/usr/bin/env bash
set -euo pipefail

read -rp "SSH user: " SSH_USER
read -rp "SSH host (e.g. fusion-sais.lisn.upsaclay.fr): " SSH_HOST
PORT=8501

echo "Running: ssh -L ${PORT}:localhost:${PORT} ${SSH_USER}@${SSH_HOST}"
ssh -L ${PORT}:localhost:${PORT} ${SSH_USER}@${SSH_HOST}
