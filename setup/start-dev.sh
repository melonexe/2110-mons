#!/usr/bin/env bash
# Quick start for development / testing without systemd.
# Run from the project root: ./setup/start-dev.sh

set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$APP_DIR/.venv"

if [[ ! -d "$VENV" ]]; then
    echo "No virtualenv found — run setup/vm-setup.sh first."
    exit 1
fi

source "$VENV/bin/activate"
cd "$APP_DIR/backend"
exec python main.py
