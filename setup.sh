#!/usr/bin/env bash
# setup.sh — one-time setup for the Family Archive on macOS.
#
# Creates a private virtualenv in ./.venv and installs the two runtime
# dependencies (pillow, pillow_heif). Needs a network connection ONCE; after
# this the archive runs entirely offline.
#
# Usage:
#   ./setup.sh          # runtime only
#   ./setup.sh --dev    # runtime + pytest, so ./run_tests.sh works

set -euo pipefail
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$_HERE"

_PY="${WYEAST_PYTHON:-}"
if [[ -z "$_PY" ]]; then
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then _PY="$(command -v "$cand")"; break; fi
  done
fi
if [[ -z "$_PY" ]]; then
  echo "setup.sh: no python3 found on PATH." >&2
  echo "  Install one from https://www.python.org/downloads/macos/ (3.10 or newer)," >&2
  echo "  or 'brew install python@3.12', then re-run ./setup.sh." >&2
  exit 1
fi

# 3.10 floor: wyeast/core/delivery.py uses PEP 604 'str | None' annotations.
"$_PY" - <<'PYEOF'
import sys
if sys.version_info < (3, 10):
    sys.exit("setup.sh: Python 3.10 or newer is required (found %s)." % sys.version.split()[0])
PYEOF

echo "==> creating .venv with $_PY"
"$_PY" -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip >/dev/null

if [[ "${1:-}" == "--dev" ]]; then
  echo "==> installing runtime + test dependencies"
  ./.venv/bin/python3 -m pip install -r requirements-dev.txt
else
  echo "==> installing runtime dependencies"
  ./.venv/bin/python3 -m pip install -r requirements.txt
fi

echo
echo "==> checking this interpreter's SQLite has FTS5 (needed for full-text search)"
if ./.venv/bin/python3 - <<'PYEOF'
import sqlite3, sys
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except sqlite3.OperationalError as e:
    sys.exit("FTS5 missing: %s" % e)
PYEOF
then
  echo "    FTS5: OK"
else
  echo "    WARNING: this Python's SQLite has no FTS5 — the Search view will not work." >&2
  echo "    Use a python.org or Homebrew Python build instead of Apple's system python3." >&2
fi

echo
echo "==> checking HEIC/HEIF decode (iPhone photos)"
./.venv/bin/python3 - <<'PYEOF'
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    print("    HEIC: OK (pillow_heif %s)" % pillow_heif.__version__)
except Exception as e:
    print("    WARNING: HEIC thumbnails will silently fail — %s" % e)
PYEOF

echo
echo "Setup complete. Next:  ./family_archive.sh CASE_ID --cases-root /path/to/cases"
