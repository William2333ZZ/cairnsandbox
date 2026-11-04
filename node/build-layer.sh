#!/bin/bash
# Build a layer from a recipe directory (docs/004-guest.md): boot a build sandbox on <template>, upload
# <recipe>/files/ (if any) to /tmp/recipe-files, run <recipe>/recipe.sh as root with the apt mirror allowed, pack the
# writable layer into a new read-only layer, record a manifest (recipe hash, template, parent layers) and clean up.
# BUILD_MEM (MiB, default 1024): pack writes its tar into the guest's /run tmpfs (half of RAM), so layers over ~400 MiB need more.
# usage: build-layer.sh <recipe-dir> [template] [parent-layer-ids(comma)] [node-url]   -> prints the layer id
set -euo pipefail
RECIPE=$(readlink -f "$1"); TPL=${2:-ubuntu-24.04}; PARENTS=${3:-}; N=${4:-http://127.0.0.1:18800}
T=${NODE_TOKEN:-$(docker exec fc-node cat /var/lib/fc-node/node-token)}; NAME=$(basename "$RECIPE")
H=(-H "Authorization: Bearer $T" -H "content-type: application/json")
LAYERS=$(python3 -c 'import sys,json;print(json.dumps([x for x in sys.argv[1].split(",") if x]))' "$PARENTS")
# APT_MIRROR (e.g. mirrors.example.com) points the recipes' apt at a regional mirror; the build sandbox may then reach only that host.
HOSTS=$(python3 -c 'import sys,json,os;m=os.environ.get("APT_MIRROR");print(json.dumps([m] if m else ["deb.debian.org","security.debian.org","archive.ubuntu.com","security.ubuntu.com"]))')
S=$(curl -sf "${H[@]}" -d "{\"template\":\"$TPL\",\"class\":\"persistent\",\"mem\":${BUILD_MEM:-1024},\"vcpu\":2,\"disk\":2048,\"ttl_idle_s\":1800,\"layers\":$LAYERS,\"network\":{\"allow_hosts\":$HOSTS}}" "$N/sandboxes" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "build sandbox $S on $TPL layers=$LAYERS" >&2
cleanup() { curl -sf -X DELETE "${H[@]}" "$N/sandboxes/$S" >/dev/null || true; }; trap cleanup EXIT
ex() { curl -sf "${H[@]}" -d "$(python3 -c 'import json,sys;print(json.dumps({"cmd":sys.argv[1],"user":"root","timeout":int(sys.argv[2])}))' "$1" "${2:-900}")" "$N/sandboxes/$S/exec" \
       | python3 -c 'import sys,json;r=json.load(sys.stdin);sys.stderr.write(r.get("stdout","")[-1500:]+r.get("stderr","")[-800:]);sys.exit(0 if r.get("exit")==0 else 1)'; }
TAR=$(mktemp); tar -C "$RECIPE" -cf "$TAR" recipe.sh $( [ -d "$RECIPE/files" ] && echo files )
curl -sf -X PUT -H "Authorization: Bearer $T" --data-binary @"$TAR" "$N/sandboxes/$S/files?path=/tmp/recipe.tar" >/dev/null; rm -f "$TAR"
ex 'mkdir -p /tmp/recipe-files && cd /tmp && tar -xf recipe.tar && { [ -d files ] && cp -R files/. /tmp/recipe-files/ || true; } && chmod +x recipe.sh' 60
ex "APT_MIRROR=${APT_MIRROR:-} bash /tmp/recipe.sh" 1800
ex 'rm -rf /tmp/recipe.tar /tmp/recipe.sh /tmp/files /tmp/recipe-files; du -sh /usr /opt 2>/dev/null | tr "\n" " "' 60
RH=$(cd "$RECIPE" && find . -type f -not -path "*/__pycache__/*" | sort | xargs sha256sum | sha256sum | cut -c1-16)
META=$(curl -sS --fail-with-body -m 3000 "${H[@]}" -d "{\"name\":\"$NAME\"}" "$N/sandboxes/$S/pack") || { echo "pack failed: $META" >&2; exit 1; }
L=$(echo "$META" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(m["id"]);print(m["bytes"]//1048576,"MiB in",m["s"],"s",file=sys.stderr)')
echo "{\"layer\":\"$L\",\"recipe\":\"$NAME\",\"recipe_hash\":\"$RH\",\"template\":\"$TPL\",\"parents\":$LAYERS,\"built\":$(date +%s)}" | tee -a "$(dirname "$0")/../state/layers-built.jsonl" >/dev/null 2>&1 || true
echo "$L"
