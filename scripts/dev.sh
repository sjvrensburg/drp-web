#!/usr/bin/env bash
# Run the app in the foreground with auto-reload, for development.
#
#   scripts/dev.sh
#
# Binds 127.0.0.1 only. Remote access is `tailscale serve` proxying to loopback (see
# README) -- this app never listens on a routable address.
#
# It refuses to start if the installed service is running: both would take the same
# state-directory flock, and the loser exits after uvicorn has already bound nothing.
set -euo pipefail

VENV="${VENV:-$HOME/venvs/drpweb}"
cd "$(dirname "$0")/.."

[ -x "$VENV/bin/uvicorn" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

exec "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8095 --reload
