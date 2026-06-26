#!/usr/bin/env bash
# mark launcher: sets up a local venv, installs deps, and starts the app.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "mark: creating virtual environment..."
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
VENV_PY="$VENV/bin/python"

"$VENV_PY" -m pip install -q --upgrade pip >/dev/null
"$VENV_PY" -m pip install -q -r requirements.txt

# Best-effort semantic-search upgrade. Falls back to the built-in vectorizer.
if [ "${MARK_SKIP_SEMANTIC:-0}" != "1" ]; then
  "$VENV_PY" -m pip install -q -r requirements-optional.txt 2>/dev/null \
    || echo "mark: optional semantic deps unavailable for this Python — using built-in vectorizer."
fi

exec "$VENV_PY" -m mark
