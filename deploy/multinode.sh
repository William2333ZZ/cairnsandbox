#!/bin/bash
# Simulate a multi-machine cluster on ONE host: N node containers (each with its own network namespace, state directory, port and token, so to the
# control plane they are separate machines) plus one control plane, registry, database and chunk store. Control-plane behaviour (placement, routing,
# migration, evacuation, reconciliation) is the same as on real machines; the physical layer is not (see "What this does not model" in deploy/README.md).
#
#   deploy/multinode.sh up N        generate the N-node compose file and start the stack (N >= 1; node 1 is fc-node, node k is fc-node-k)
#   deploy/multinode.sh status      the control plane's view of the nodes
#   deploy/multinode.sh kill K      SIGKILL node K: a machine that lost power (no graceful pause)
#   deploy/multinode.sh start K     bring node K back
#   deploy/multinode.sh partition K cut node K off the network (the process keeps running); heal K reconnects it
#   deploy/multinode.sh evacuate K  ask the control plane to move node K's durable sandboxes elsewhere
#   deploy/multinode.sh down [-v]   stop and remove the stack (-v also deletes the compose volumes; data directories stay)
#
# Environment (all optional):
#   MULTINODE_PROJECT   compose project name, default cairn-multi (keeps it apart from a normal `docker compose up` in this directory)
#   PORT_OFFSET         added to every published host port, default 0; use e.g. 100 when another CairnSandbox stack already owns 18700/18800/...
#   MEM_BUDGET_MIB      memory budget PER NODE; default: half of the host's RAM split across the nodes, at most 8192 each
#   MAX_VMS             VM cap per node (default from .env / 32)
#   STORE_BW_MBPS       throttle the shared chunk store (simulates the link between machines; affects bundle migration and lazy image reads)
#   FENCE_AFTER_S, AUTO_EVACUATE_AFTER_S   see .env.example (self-fencing nodes, automatic evacuation)
# Needs the same things as `docker compose up` (deploy/README.md): images built, deploy/.env, ./make-secrets.sh. Data lives under DATA_DIR/state, state-2, ...
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"
PROJECT=${MULTINODE_PROJECT:-cairn-multi}; OFFSET=${PORT_OFFSET:-0}; OUT=.multinode.compose.json
node_name() { if [ "$1" = 1 ]; then echo fc-node; else echo "fc-node-$1"; fi; }
dc() { docker compose -p "$PROJECT" -f "$OUT" "$@"; }
need_up() { [ -f "$OUT" ] || { echo "no stack generated yet: run  $0 up N" >&2; exit 1; }; }
token() { cat "${SECRETS_DIR:-./secrets}/cp.token"; }
api() { curl -fsS -H "Authorization: Bearer $(token)" "$@"; }
cp_url() { python3 -I -c '
import json
for p in json.load(open(".multinode.compose.json"))["services"]["fc-cp"]["ports"]:
    if p["target"] == 8700: print("http://127.0.0.1:" + p["published"])'; }
net_of() { docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$1"; }

generate() {
  local n=$1 tmp; tmp=$(mktemp)
  docker compose -f docker-compose.yml config --format json > "$tmp"
  MEMKB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 16777216) \
  python3 -I - "$tmp" "$n" "$PROJECT" "$OFFSET" > "$OUT" <<'PY'
import copy, json, os, sys
cfg_path, n, project, offset = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
cfg = json.load(open(cfg_path)); svcs = cfg["services"]
cfg["name"] = project
per = int(os.environ.get("MEM_BUDGET_MIB") or min(8192, int(os.environ["MEMKB"]) // 1024 // 2 // n))
base = svcs["fc-node"]; base["environment"]["MEM_BUDGET_MIB"] = str(per)
nodes = [{"name": "fc-node", "url": "http://fc-node:8800", "token_file": "/nodes/state/node-token"}]
for k in range(2, n + 1):
    name = f"fc-node-{k}"; s = copy.deepcopy(base)
    s["hostname"] = name; s["environment"]["NODE_NAME"] = name
    s["ports"] = [dict(p, published=str(int(p["published"]) + k - 1)) for p in base["ports"]]   # next to node 1's port
    for v in s["volumes"]:
        if v["target"] == "/var/lib/fc-node": v["source"] = v["source"] + f"-{k}"
    svcs[name] = s
    nodes.append({"name": name, "url": f"http://{name}:8800", "token_file": f"/nodes/state-{k}/node-token"})
cp = svcs["fc-cp"]
cp["environment"]["NODES"] = json.dumps(nodes, separators=(",", ":"))
cp["depends_on"] = dict(cp.get("depends_on", {}), **{nd["name"]: {"condition": "service_started", "required": True} for nd in nodes})
for s in svcs.values():                       # keep clear of any other stack on this host
    for p in s.get("ports", []): p["published"] = str(int(p["published"]) + offset)
for nw in cfg.get("networks", {}).values():
    if "name" in nw: nw["name"] = f"{project}_default"
json.dump(cfg, sys.stdout, indent=1)
PY
  rm -f "$tmp"
}

case "${1:-}" in
  up)
    N=${2:?usage: multinode.sh up N}; [[ "$N" =~ ^[0-9]+$ ]] && [ "$N" -ge 1 ] || { echo "N must be a positive integer" >&2; exit 2; }
    [ -f .env ] || cp .env.example .env
    [ -f "${SECRETS_DIR:-./secrets}/cp.token" ] || ./make-secrets.sh
    generate "$N"
    dc up -d
    CP=$(cp_url)
    for _ in $(seq 1 60); do api -m 3 "$CP/health" >/dev/null 2>&1 && break; sleep 2; done
    echo "control plane $CP   token: ${SECRETS_DIR:-./secrets}/cp.token   nodes: $N"
    "$0" status ;;
  status)
    need_up; CP=$(cp_url); api -m 10 "$CP/nodes" | python3 -I -c '
import json, sys
for n in json.load(sys.stdin)["nodes"]:
    h = n.get("health") or {}
    print("%-12s %-5s vms=%s/%s mem=%s/%s MiB%s" % (n["name"], "up" if n.get("alive") else "DOWN", h.get("vms_running", "-"), h.get("max_vms", "-"), h.get("mem_mib_running", "-"), h.get("mem_budget_mib", "-"), "  cordoned" if n.get("cordoned") else ""))' ;;
  kill)      need_up; dc kill "$(node_name "${2:?node number}")" ;;
  start)     need_up; dc start "$(node_name "${2:?node number}")" ;;
  partition) need_up; C=$(dc ps -q "$(node_name "${2:?node number}")"); docker network disconnect "$(net_of "$C")" "$C" ;;
  heal)      need_up; S=$(node_name "${2:?node number}"); C=$(dc ps -q "$S"); docker network connect --alias "$S" "$PROJECT"_default "$C" ;;
  evacuate)  need_up; CP=$(cp_url); api -m 600 -X POST "$CP/nodes/$(node_name "${2:?node number}")/evacuate"; echo ;;
  down)      need_up; dc down ${2:+"$2"} ;;
  *)         sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
