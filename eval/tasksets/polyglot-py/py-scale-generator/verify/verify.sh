#!/usr/bin/env bash
# $1 = workspace. Copies the hidden tests next to the solution and runs pytest; last line is `SCORE x`.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; WS="$(cd "$1" && pwd)"
T="$(mktemp -d)"; cp -R "$WS"/. "$T"/; find "$D" -maxdepth 1 -type f ! -name verify.sh -exec cp {} "$T"/ \;    # every hidden test file, helpers included
cd "$T"
OUT="$(python3 -I -m pytest -q -p no:cacheprovider 2>&1 | tail -40)"       # the verify sandbox already bounds wall time
echo "$OUT" | tail -15
P=$(echo "$OUT" | grep -Eo '[0-9]+ passed' | tail -1 | grep -Eo '[0-9]+' || echo 0)
F=$(echo "$OUT" | grep -Eo '[0-9]+ (failed|error)' | awk '{s+=$1} END{print s+0}')
N=$((P + F)); rm -rf "$T"
if [ "$N" -eq 0 ]; then echo "SCORE 0"; exit 0; fi
python3 -c "print('SCORE', round($P/$N, 4))"
