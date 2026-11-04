#!/bin/bash
# Stage interpreters from images/dl (downloaded on a machine with GitHub access) and build the layer on <template>.
# usage: build.sh [template] [node-url]  -> prints the layer id; writes state/langs-layer-id.<template>
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")"); REPO=$(readlink -f "$HERE/../.."); TPL=${1:-debian-13}; N=${2:-http://127.0.0.1:18800}
F="$HERE/files"; rm -rf "$F"; mkdir -p "$F/wheels"
cp "$REPO"/images/dl/cpython-*.tar.gz "$F"/; cp "$REPO"/images/dl/*.whl "$F/wheels/"; cp "$REPO/images/dl/bun" "$F/bun"
L=$("$REPO/node/build-layer.sh" "$HERE" "$TPL" "" "$N")
echo "$L" | tee "$REPO/state/langs-layer-id.$TPL"
