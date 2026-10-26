#!/usr/bin/env bash
# QA Forge Studio for macOS / Linux: ./start_studio.sh  (first run sets everything up)
set -e
cd "$(dirname "$0")"
VPY=.venv/bin/python
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "No supported Python 3.10+ interpreter found. Install Python 3.10+ and try again." >&2
  exit 1
fi

if [ ! -x "$VPY" ] || ! "$VPY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "First start: setting up QA Forge (only once)..."
  rm -rf .venv
  "$PYTHON_BIN" -m venv .venv
fi

if ! cmp -s requirements.txt .venv/requirements.installed; then
  "$VPY" -m pip install -q --upgrade pip
  "$VPY" -m pip install -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi
[ -f .venv/browser.ready ] || { "$VPY" -m playwright install chromium && touch .venv/browser.ready; }
echo "Starting Studio at http://127.0.0.1:8700 (Ctrl+C to stop)"
exec "$VPY" -m qaforge studio --open
