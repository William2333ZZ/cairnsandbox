#!/bin/bash
# Build the harness layer on top of the agent-langs layer (needed for the selfcheck). usage: build.sh [template] [node-url]
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")"); REPO=$(readlink -f "$HERE/../.."); TPL=${1:-debian-13}; N=${2:-http://127.0.0.1:18800}
LANGS=$(cat "$REPO/state/langs-layer-id.$TPL"); F="$HERE/files"; rm -rf "$F"; mkdir -p "$F"
cp -R "$REPO/aios" "$F/aios"; rm -rf "$F/aios/__pycache__"
L=$("$REPO/node/build-layer.sh" "$HERE" "$TPL" "$LANGS" "$N")
echo "$L" | tee "$REPO/state/harness-layer-id.$TPL"
