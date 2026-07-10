#!/usr/bin/env bash
# Activate the venv and launch the Costco receipt web app.
# Usage: ./run_web.sh [--port 8000] [--host 127.0.0.1]
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "No .venv found. Creating one and installing requirements..."
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  python -m playwright install chromium
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Pass through any flags (e.g. --port 9000). Otherwise use PORT (default 8000).
if [ "$#" -gt 0 ]; then
  exec python -m costco_archiver web "$@"
else
  PORT="${PORT:-8000}"
  echo "Starting web app on http://127.0.0.1:${PORT} (Ctrl-C to stop)"
  exec python -m costco_archiver web --port "${PORT}"
fi
