#!/bin/bash
# usage: build.sh [template] [node-url]  -> prints the layer id; writes state/gotool-layer-id.<template>
# files/go.tar.gz (linux-amd64) and files/modcache.tar.gz are prepared on a machine with internet and not committed.
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")"); REPO=$(readlink -f "$HERE/../.."); TPL=${1:-debian-13}; N=${2:-http://127.0.0.1:18800}
L=$("$REPO/node/build-layer.sh" "$HERE" "$TPL" "$(cat "$REPO/state/dev-layer-id.$TPL")" "$N")
echo "$L" | tee "$REPO/state/gotool-layer-id.$TPL"
