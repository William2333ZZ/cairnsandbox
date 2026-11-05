#!/bin/bash
# Run ONE node on this machine and attach it to a control plane on another machine (docs/en/multi-machine.md).
#   deploy/node.sh init NAME BIND_IP STORE_URL    write node.env: node name, this machine's private IP, the control plane side's chunk store URL, a fresh token
#   deploy/node.sh up | down | logs               start / stop / follow the node
#   deploy/node.sh register CP_URL CP_TOKEN_FILE  tell the control plane about this node (POST /nodes with this node's URL and token)
# Optional environment for `init`: NODE_PORT (8800), MEM_BUDGET_MIB, MAX_VMS, REPO_DIR (..), NODE_IMAGE (fc-node:dev).
# This machine needs: Docker, /dev/kvm and friends (same as any node), the fc-node image, a checkout of this repo (recipes/templates) and the one small file
# images/layers/upper-template.ext4 (copy it from the control plane machine). It does NOT need the kernel or base image: they are read from the chunk store.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"
dc() { docker compose --env-file node.env -f docker-compose.node.yml "$@"; }
need_env() { [ -f node.env ] || { echo "no node.env: run  $0 init NAME BIND_IP STORE_URL" >&2; exit 1; }; }
case "${1:-}" in
  init)
    NAME=${2:?usage: node.sh init NAME BIND_IP STORE_URL}; IP=${3:?BIND_IP}; STORE=${4:?STORE_URL}
    [ ! -f node.env ] || { echo "node.env already exists; delete it to start over (the token is in it)" >&2; exit 1; }
    case "$IP" in 0.0.0.0|"") echo "BIND_IP must be this machine's private address, not 0.0.0.0" >&2; exit 2;; esac
    ( umask 077
      { echo "NODE_NAME=$NAME"; echo "BIND_IP=$IP"; echo "STORE_URL=$STORE"; echo "NODE_PORT=${NODE_PORT:-8800}"
        echo "NODE_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        for v in MEM_BUDGET_MIB MAX_VMS REPO_DIR NODE_IMAGE FENCE_AFTER_S; do [ -z "${!v:-}" ] || echo "$v=${!v}"; done; } > node.env )
    echo "wrote node.env (mode 0600). Next: $0 up   then   $0 register <control plane URL> <its token file>" ;;
  up)       need_env; mkdir -p state
            R=$(grep '^REPO_DIR=' node.env | cut -d= -f2-); R=${R:-..}
            [ -f "$R/images/layers/upper-template.ext4" ] || { echo "missing $R/images/layers/upper-template.ext4: copy it from the control plane machine's images/layers/ (rsync -S keeps it sparse)" >&2; exit 1; }
            dc up -d ;;
  down)     need_env; dc down ;;
  logs)     need_env; dc logs -f --tail 50 ;;
  register)
    need_env; CP=${2:?usage: node.sh register CP_URL CP_TOKEN_FILE}; TF=${3:?CP_TOKEN_FILE}
    set -a; . ./node.env; set +a
    BODY=$(python3 -I -c 'import json,os; print(json.dumps({"name": os.environ["NODE_NAME"], "url": "http://%s:%s" % (os.environ["BIND_IP"], os.environ.get("NODE_PORT", "8800")), "token": os.environ["NODE_TOKEN"]}))')
    curl -fsS -X POST "$CP/nodes" -H "Authorization: Bearer $(cat "$TF")" -H 'Content-Type: application/json' -d "$BODY"; echo ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
