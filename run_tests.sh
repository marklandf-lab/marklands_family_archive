#!/usr/bin/env bash
# run_tests.sh — run the carried-over unit tests against this checkout.
# Requires ./setup.sh --dev (pytest). Nothing here touches a real case.
set -euo pipefail
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$_HERE"
_PY="${WYEAST_PYTHON:-}"
if [[ -z "$_PY" && -x .venv/bin/python3 ]]; then _PY=.venv/bin/python3; fi
if [[ -z "$_PY" ]]; then _PY="$(command -v python3)"; fi
exec "$_PY" -m pytest tests/ "$@"
