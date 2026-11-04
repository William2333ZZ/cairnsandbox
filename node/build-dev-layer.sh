#!/bin/bash
# Compatibility wrapper: the dev layer is now the recipe images/recipes/dev-tools (see node/build-layer.sh).
# usage: build-dev-layer.sh [node-url] [aios-dir] [template]   -> writes state/dev-layer-id
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")"); N=${1:-http://127.0.0.1:18800}; Cairn=${2:-$HERE/../aios}; TPL=${3:-ubuntu-24.04}
L=$("$HERE/build-layer.sh" "$HERE/../recipes/dev-tools" "$TPL" "" "$N")
echo "$L" | tee "$HERE/../state/dev-layer-id${TPL:+.$TPL}"
[ "$TPL" = ubuntu-24.04 ] && echo "$L" > "$HERE/../state/dev-layer-id" || true
