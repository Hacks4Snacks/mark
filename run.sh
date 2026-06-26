#!/usr/bin/env bash
# mark launcher: sets up a local venv, installs deps, and starts the app.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "mark: creating virtual environment…"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

pip install -q --upgrade pip >/dev/null
pip install -q -r requirements.txt

# Best-effort semantic-search upgrade. Falls back to the built-in vectorizer.
if [ "${MARK_SKIP_SEMANTIC:-0}" != "1" ]; then
  pip install -q -r requirements-optional.txt 2>/dev/null \
    || echo "mark: optional semantic deps unavailable for this Python — using built-in vectorizer."
fi

exec python -m mark
