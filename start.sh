#!/usr/bin/env bash
# Double-click once (macOS: start.command; Linux: ./start.sh). After that Tradewise starts by itself
# when you log in, and the "Tradewise" icon on your desktop opens it.
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then echo "Python 3.10+ is needed: https://www.python.org/downloads/"; read -r -p "Press Enter to close"; exit 1; fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Tradewise (about a minute)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[yahoo]"
fi
if ! .venv/bin/python -m tradewise.cli start "$@"; then read -r -p "Something went wrong (see above). Press Enter to close"; exit 1; fi
sleep 3
