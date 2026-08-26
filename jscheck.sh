#!/usr/bin/env bash
# jscheck.sh — parse-check JavaScript with JavaScriptCore.
#
# There is no node on this Mac (see CLAUDE.md, "macOS caveats that bite"), and
# report_assets/family/family.js is now several thousand lines of hand-edited
# code that nothing else validates — pytest cannot see it at all. `new Function()`
# forces a full parse without executing a line, which catches every syntax error
# in about a second.
#
#   ./jscheck.sh report_assets/family/family.js
#
# Exits non-zero if any file fails to parse, so it chains: ./jscheck.sh x && ./run_tests.sh
set -uo pipefail
rc=0
for f in "$@"; do
  out=$(osascript -l JavaScript -e "
    ObjC.import('Foundation');
    var s = ObjC.unwrap(\$.NSString.stringWithContentsOfFileEncodingError('$f', 4, null));
    if (!s) { 'CANNOT READ' } else { try { new Function(s); 'OK' } catch (e) { 'SYNTAX ERROR: ' + e.message } }
  " 2>&1)
  echo "$f: $out"
  [[ "$out" == "OK" ]] || rc=1
done
exit $rc
