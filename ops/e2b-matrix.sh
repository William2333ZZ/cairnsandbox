#!/bin/bash
# Run the E2B conformance suites against several SDK versions, each in its own throwaway environment, and print one table.
#
#   E2B_API_URL=... E2B_SANDBOX_URL=... E2B_API_KEY=... [CAIRN_URL=... CAIRN_TOKEN=... E2B_PREVIEW_DOMAIN=...] \
#     ops/e2b-matrix.sh <workdir> "py:2.63.0 py:2.60.0 npm:2.62.2 ..." [results-dir]
#
# Needs python3 (>= 3.10) and node/npm and network access to PyPI / npm (it installs the packages). The exit code is the number of versions with a FAIL.
# A version that cannot run a case because its SDK lacks that API reports SKIP for it (the suite checks), never a silent pass.
set -uo pipefail
WORK=${1:?workdir}; VERSIONS=${2:?versions like "py:2.63.0 npm:2.62.2"}; RES=${3:-$WORK/results}
HERE=$(cd "$(dirname "$0")" && pwd); mkdir -p "$WORK" "$RES"; PY=${PYTHON:-python3}; bad=0
printf '%-14s %6s %6s %6s  %s\n' sdk pass fail skip note
for v in $VERSIONS; do
  kind=${v%%:*}; ver=${v#*:}; out="$RES/$kind-$ver.json"; note=""
  if [ "$kind" = py ]; then
    d="$WORK/py-$ver"; [ -x "$d/bin/python" ] || { "$PY" -m venv "$d" && "$d/bin/pip" install -q "e2b==$ver" >/dev/null 2>&1; } || note="install failed"
    [ -x "$d/bin/python" ] && "$d/bin/python" -c "import e2b" 2>/dev/null || note="install failed"
    [ -z "$note" ] && "$d/bin/python" "$HERE/e2b-conformance.py" --json "$out" >"$RES/$kind-$ver.log" 2>&1
  else
    d="$WORK/npm-$ver"; mkdir -p "$d"; [ -d "$d/node_modules/e2b" ] || (cd "$d" && npm init -y >/dev/null 2>&1 && npm install --silent "e2b@$ver" >/dev/null 2>&1) || note="install failed"
    [ -d "$d/node_modules/e2b" ] || note="install failed"
    if [ -z "$note" ]; then cp "$HERE/e2b-conformance.mjs" "$d/"; (cd "$d" && node e2b-conformance.mjs --json "$out" >"$RES/$kind-$ver.log" 2>&1); fi
  fi
  if [ -s "$out" ]; then
    read -r p f s < <(python3 -c "import json,sys;d=json.load(open('$out'))['summary'];print(d['PASS'],d['FAIL'],d['SKIP'])")
    [ "$f" -gt 0 ] && bad=$((bad+1)) && note="see $RES/$kind-$ver.log"
    printf '%-14s %6s %6s %6s  %s\n' "$kind $ver" "$p" "$f" "$s" "$note"
  else
    bad=$((bad+1)); printf '%-14s %6s %6s %6s  %s\n' "$kind $ver" - - - "${note:-did not run: see $RES/$kind-$ver.log}"
  fi
done
exit $bad
