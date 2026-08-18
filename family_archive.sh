#!/usr/bin/env bash
# family_archive.sh — launch the Wyeast Family Archive local server on macOS.
#
# The macOS counterpart of Zone B's tools/family_archive.sh. Same tool, same
# verbs, same audit trail; the only differences are which interpreter it picks
# and how it decides where the case tree lives.
#
# Usage:
#   ./family_archive.sh CASE_001                          # examiner role, port 7766
#   ./family_archive.sh CASE_001 --role family
#   ./family_archive.sh CASE_001 --port 7777
#   ./family_archive.sh CASE_001 --cases-root /Volumes/WyeastUSB/cases
#
# Interpreter, in order:  $WYEAST_PYTHON -> ./.venv/bin/python3 -> python3 on PATH
# Cases root, in order:   --cases-root -> $WYEAST_CASES_ROOT -> ./cases -> ~/WyeastCases
#
# Exit codes mirror family_archive.py:
#   0 served · 1 bad args/missing case · 2 not complete · 3 family blocked

set -euo pipefail
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── interpreter ──────────────────────────────────────────────────────────────
_PY="${WYEAST_PYTHON:-}"
if [[ -z "$_PY" && -x "$_HERE/.venv/bin/python3" ]]; then
  _PY="$_HERE/.venv/bin/python3"
fi
if [[ -z "$_PY" ]]; then
  _PY="$(command -v python3 || true)"
fi
if [[ -z "$_PY" ]]; then
  echo "family_archive.sh: no Python found. Run ./setup.sh first." >&2
  exit 1
fi
if [[ ! -x "$_HERE/.venv/bin/python3" && -z "${WYEAST_PYTHON:-}" ]]; then
  echo "family_archive.sh: no ./.venv — using $_PY from PATH." >&2
  echo "  If pillow/pillow_heif are not installed there, run ./setup.sh." >&2
fi

# ── cases root ───────────────────────────────────────────────────────────────
# Only injected when the caller did not pass --cases-root themselves. Without
# it the tool would fall back to pipeline_config.json's Zone B path (/data/cases),
# which does not exist on a Mac.
_want_root=1
for a in "$@"; do
  case "$a" in
    --cases-root|--cases-root=*) _want_root=0 ;;
  esac
done

_extra=()
if (( _want_root )); then
  if [[ -n "${WYEAST_CASES_ROOT:-}" ]]; then
    _ROOT="$WYEAST_CASES_ROOT"
  elif [[ -d "$_HERE/cases" ]]; then
    _ROOT="$_HERE/cases"
  else
    _ROOT="$HOME/WyeastCases"
  fi
  if [[ ! -d "$_ROOT" ]]; then
    echo "family_archive.sh: cases root '$_ROOT' does not exist." >&2
    echo "  Point at your curation bundle with --cases-root /path/to/cases," >&2
    echo "  set WYEAST_CASES_ROOT, or put the case under $HOME/WyeastCases/." >&2
    exit 1
  fi
  _extra=(--cases-root "$_ROOT")
fi

# PYTHONPATH (rather than cd) so the caller's relative paths still resolve.
export PYTHONPATH="$_HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "$_PY" -m tools.family_archive "$@" "${_extra[@]+"${_extra[@]}"}"
