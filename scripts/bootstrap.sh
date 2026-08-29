#!/usr/bin/env bash
# Create (or recreate) the virtualenv this app runs in.
#
#   scripts/bootstrap.sh [--force]
#
# Its own venv, not ~/venvs/workbench: halo-workbench's environment carries pymupdf and
# onnxruntime for its redaction tool, and nothing here needs them. And emphatically not
# the pipeline's environment -- ../deep-research-podcast is stdlib-only by rule, runs
# under the system python3, and must keep running when this app is not installed at all.
set -euo pipefail

VENV="${VENV:-$HOME/venvs/drpweb}"

if [ "${1:-}" = "--force" ] && [ -d "$VENV" ]; then
    echo ">> removing $VENV"
    rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
    echo ">> creating $VENV"
    python3 -m venv "$VENV"
fi

echo ">> installing dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
# fastapi + uvicorn: the app. jinja2: server-rendered pages, no build step and no npm.
# python-multipart: FastAPI refuses file uploads without it, with an error that reads
# like a bug in your own code. Nothing else -- every call this app makes to a model, to
# Open Notebook or to systemd goes through urllib or a subprocess, the same way the
# pipeline does it.
"$VENV/bin/pip" install --quiet fastapi 'uvicorn[standard]' jinja2 python-multipart

"$VENV/bin/python" - <<'PY'
import fastapi, uvicorn, jinja2
print(f">> fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}, jinja2 {jinja2.__version__}")
PY

cat <<'MSG'

>> Done.

   Run it:    scripts/dev.sh
   Check it:  curl -s localhost:8095/healthz
   Tests:     tests/acceptance.sh
MSG
