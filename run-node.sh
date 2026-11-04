#!/bin/bash
# Node agent / chunk store / control plane launcher.
#   ./run-node.sh                 node "fc-node"   (state/,   host port 18800)   NODE=2 ./run-node.sh -> fc-node-2 (state-2/, 18802)
#   ./run-node.sh store [mbps]    chunk store "fc-store" on fcnet (store/, 18900); STORE_BACKEND=s3 puts chunks in the fc-s3 bucket instead
#   ./run-node.sh cp 0 2 3 ...    control plane "fc-cp" over the listed nodes (0 = fc-node) (cp-state/, 18700); serves web/ at /ui/, forwards /eval/*
#                                 Go binary in image fc-cp:dev (docker build -t fc-cp:dev cp/). CP_REGISTRY=redis://fc-registry:6379 shares routes, users and
#                                 registered nodes between replicas (default when the fc-registry container runs; CP_REGISTRY=file forces files in cp-state/).
#                                 Another replica: CP_NAME=fc-cp-b CP_HOST_PORT=18720 ./run-node.sh cp 0 2 3. The E2B-shaped surface (D40) is on host port <cp port>+10 (18710).
#   ./run-node.sh registry        the replicas' shared Redis "fc-registry" (registry-data/, 127.0.0.1:16379), appendonly
#   ./run-node.sh db              the replicas' shared Postgres "fc-db" (db-data/, 127.0.0.1:15432; password in SECRETS/db.password): accounts, projects,
#                                 members, quotas, usage events. CP_DB=postgres://cp:<pw>@fc-db:5432/cp is the default when fc-db runs; CP_DB=sqlite forces cp-state/cp.sqlite
#   ./run-node.sh model           model API proxy "fc-model" (model-state/, 18500); MODEL_BACKEND=mock|upstream,
#                                 UPSTREAM_URL=https://api.deepseek.com/v1 UPSTREAM_KEY_FILE=/secrets/deepseek.key (from SECRETS_DIR)
#   ./run-node.sh eval            evaluator "fc-eval" (eval-state/, 18600) against fc-cp and fc-model
#   ./run-node.sh intake          intake layer "fc-intake" (intake-state/, 18400): GITEA_REPO=William2333ZZ/cairn GITEA_TOKEN_FILE=/secrets/gitea-cairn-bot.token
# Env for nodes: JAIL (1), LAZY_STORE (e.g. http://fc-store:8900), MAX_VMS (32), MEM_BUDGET_MIB (8192), FENCE_AFTER_S (D54, 0=off), TEMPLATE_POOL, REGISTRY_MIRROR (OCI pulls; default https://registry-1.docker.io).
# JAIL=1 runs every VM under the Firecracker jailer (chroot, unprivileged uid, per-VM cgroup); that needs SYS_ADMIN (mounts,
# pivot_root), SYS_PTRACE (core-sched cookie on the jailed VMM), SYS_NICE (qos changes), a private cgroup namespace and
# Docker's default AppArmor/seccomp off for this (trusted) node container.
cd "$(cd "$(dirname "$0")" && pwd)"          # everything below is relative to the checkout
SFX=${SVC_SUFFIX:-}; PO=${PORT_OFFSET:-0}        # SVC_SUFFIX=-ci PORT_OFFSET=1: fc-model-ci/fc-cp-ci/fc-eval-ci on 18501/18701/18601 (CI's own stack)
docker network inspect fcnet >/dev/null 2>&1 || docker network create fcnet >/dev/null
# One bearer for the control plane, the Agent service and the evaluator (docs/009 鉴权与隔离): generated once into the secrets dir.
SECRETS=${SECRETS_DIR:-$PWD/secrets}; mkdir -p "$SECRETS"
if [ -z "$CP_TOKEN" ] && [ -z "$SFX" ]; then
  [ -f "$SECRETS/cp.token" ] || { umask 077; head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$SECRETS/cp.token"; }
  CP_TOKEN=$(cat "$SECRETS/cp.token")
fi
[ "$CP_TOKEN" = none ] && CP_TOKEN=""          # CP_TOKEN=none: start this service without a bearer (while other services still run without one)

if [ "$1" = "store" ]; then
  docker rm -f fc-store >/dev/null 2>&1 || true
  S3ENV=()                                      # STORE_BACKEND=s3: 块存到 fc-s3(Garage)的桶里,密钥从 $SECRETS/s3.env 进环境变量,不上命令行
  if [ "$STORE_BACKEND" = s3 ]; then S3F=$(umask 077; mktemp); grep '^S3_' "$SECRETS/s3.env" > "$S3F"; S3ENV=(--env-file "$S3F"); fi
  docker run -d --name fc-store --network fcnet "${S3ENV[@]}" -v "$PWD/store:/store" -v "$PWD/node/imgstore.py:/usr/local/bin/imgstore.py:ro" -p 127.0.0.1:18900:8900 \
    --entrypoint /usr/bin/python3 fc-node:dev /usr/local/bin/imgstore.py serve /store 8900 --bw-mbps ${2:-0} >/dev/null
  [ -n "$S3F" ] && rm -f "$S3F"
  exit 0
fi

if [ "$1" = "cp" ]; then
  shift; NODES="["; sep=""
  for n in "$@"; do                                   # 0 = the main node fc-node (state/)
    if [ "$n" = 0 ]; then NM=fc-node; ST=state; else NM=fc-node-$n; ST=state-$n; fi
    NODES="$NODES$sep{\"name\":\"$NM\",\"url\":\"http://$NM:8800\",\"token_file\":\"/nodes/$ST/node-token\"}"; sep=","
  done
  NODES="$NODES]"; NAME=${CP_NAME:-fc-cp$SFX}; HP=${CP_HOST_PORT:-$((18700+PO))}; mkdir -p cp-state$SFX console-dist
  REG=${CP_REGISTRY:-}
  if [ -z "$REG" ] && [ -z "$SFX" ] && docker inspect fc-registry >/dev/null 2>&1; then REG=redis://fc-registry:6379; fi   # the production replicas share it; CI's own stack uses files
  [ "$REG" = file ] && REG=""
  DBURL=${CP_DB:-}
  if [ -z "$DBURL" ] && [ -z "$SFX" ] && docker inspect fc-db >/dev/null 2>&1 && [ -f "$SECRETS/db.password" ]; then DBURL="postgres://cp:$(cat "$SECRETS/db.password")@fc-db:5432/cp?sslmode=disable"; fi
  [ "$DBURL" = sqlite ] && DBURL=""
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --network fcnet -v "$PWD:/nodes:ro" -v "$PWD/cp-state$SFX:/var/lib/fc-cp" -v "$PWD/web:/web:ro" -v "$PWD/console-dist:/console:ro" \
    -e NODES="$NODES" -e STORE=${STORE:-http://fc-store:8900} -e EVAL_URL=${EVAL_URL:-http://fc-eval$SFX:8600} -e INTAKE_URL=${INTAKE_URL:-http://fc-intake$SFX:8400} ${CP_TOKEN:+-e CP_TOKEN=$CP_TOKEN} \
    -e CP_REGISTRY="$REG" -e CP_REPLICA="$NAME" -e CP_DB="$DBURL" -e MODEL_PROXY_URL=${MODEL_PROXY_URL:-http://fc-model$SFX:8500} ${PROXY_ADMIN_TOKEN:+-e PROXY_ADMIN_TOKEN=$PROXY_ADMIN_TOKEN} \
    -e CP_E2B_PORT=8710 ${CP_E2B_DEFAULT_TEMPLATE:+-e CP_E2B_DEFAULT_TEMPLATE=$CP_E2B_DEFAULT_TEMPLATE} -p 127.0.0.1:$HP:8700 -p 127.0.0.1:$((HP+10)):8710 fc-cp:dev >/dev/null
  exit 0
fi

if [ "$1" = "db" ]; then
  mkdir -p db-data; [ -f "$SECRETS/db.password" ] || { umask 077; head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$SECRETS/db.password"; }
  docker rm -f fc-db >/dev/null 2>&1 || true
  docker run -d --name fc-db --network fcnet -v "$PWD/db-data:/var/lib/postgresql/data" -e POSTGRES_USER=cp -e POSTGRES_DB=cp -e POSTGRES_PASSWORD="$(cat "$SECRETS/db.password")" \
    -p 127.0.0.1:15432:5432 postgres:16-alpine >/dev/null
  exit 0
fi

if [ "$1" = "registry" ]; then
  mkdir -p registry-data; docker rm -f fc-registry >/dev/null 2>&1 || true
  docker run -d --name fc-registry --network fcnet -v "$PWD/registry-data:/data" -p 127.0.0.1:16379:6379 valkey/valkey:7.2-alpine valkey-server --appendonly yes --save "" >/dev/null
  exit 0
fi

if [ "$1" = "model" ]; then
  mkdir -p model-state$SFX; docker rm -f fc-model$SFX >/dev/null 2>&1 || true
  SECRETS=${SECRETS_DIR:-$PWD/secrets}      # upstream key file lives outside the repo, mounted read-only, never put in env
  docker run -d --name fc-model$SFX --network fcnet -v "$PWD/model-state$SFX:/var/lib/fc-model" -v "$SECRETS:/secrets:ro" -p 127.0.0.1:$((18500+PO)):8500 \
    -e MODEL_BACKEND=${MODEL_BACKEND:-mock} ${UPSTREAM_URL:+-e UPSTREAM_URL=$UPSTREAM_URL} ${UPSTREAM_KEY_FILE:+-e UPSTREAM_KEY_FILE=$UPSTREAM_KEY_FILE} ${UPSTREAM_KEY_FILES:+-e UPSTREAM_KEY_FILES="$UPSTREAM_KEY_FILES"} \
    ${DEFAULT_MODEL:+-e DEFAULT_MODEL=$DEFAULT_MODEL} ${MODEL_PRICES:+-e MODEL_PRICES=$MODEL_PRICES} ${PROXY_ADMIN_TOKEN:+-e PROXY_ADMIN_TOKEN=$PROXY_ADMIN_TOKEN} \
    --entrypoint /usr/bin/python3 fc-node:dev /usr/local/bin/modelproxy.py >/dev/null
  exit 0
fi

if [ "$1" = "eval" ]; then
  mkdir -p eval-state$SFX; docker rm -f fc-eval$SFX >/dev/null 2>&1 || true
  docker run -d --name fc-eval$SFX --network fcnet -v "$PWD/eval:/eval:ro" -v "$PWD/eval-state$SFX:/var/lib/fc-eval" -p 127.0.0.1:$((18600+PO)):8600 \
    -e EVAL_STATE=/var/lib/fc-eval -e TASKSETS_DIR=/eval/tasksets -e EVAL_TEMPLATE=${EVAL_TEMPLATE:-debian-13} -e CP_URL=${CP_URL:-http://fc-cp$SFX:8700} ${CP_TOKEN:+-e CP_TOKEN=$CP_TOKEN} \
    -e MODEL_PROXY_URL=${MODEL_PROXY_URL:-http://fc-model$SFX:8500} -e INTAKE_URL=${INTAKE_URL:-http://fc-intake$SFX:8400} -e EVAL_LAYERS=${EVAL_LAYERS:-$(T=${EVAL_TEMPLATE:-debian-13}; G=$(cat state/langs-layer-id.$T 2>/dev/null); H=$(cat state/harness-layer-id.$T 2>/dev/null); D=$(cat state/dev-layer-id.$T 2>/dev/null || cat state/dev-layer-id 2>/dev/null); echo "${G:+$G,}${H:+$H,}$D")} ${PROXY_ADMIN_TOKEN:+-e PROXY_ADMIN_TOKEN=$PROXY_ADMIN_TOKEN} -e MAX_PARALLEL=${MAX_PARALLEL:-2} \
    --entrypoint /usr/bin/python3 fc-node:dev /eval/evaluator.py serve >/dev/null
  exit 0
fi

if [ "$1" = "intake" ]; then
  mkdir -p intake-state$SFX; docker rm -f fc-intake$SFX >/dev/null 2>&1 || true
  SECRETS=${SECRETS_DIR:-$PWD/secrets}      # the Gitea agent-account token is a file mounted read-only; it never enters a sandbox
  LAYERS=${INTAKE_LAYERS:-$(T=${INTAKE_TEMPLATE:-debian-13}; G=$(cat state/langs-layer-id.$T 2>/dev/null); H=$(cat state/harness-layer-id.$T 2>/dev/null); D=$(cat state/dev-layer-id.$T 2>/dev/null); echo "${G:+$G,}${H:+$H,}$D")}
  docker run -d --name fc-intake$SFX --network fcnet -v "$PWD/intake:/intake:ro" -v "$PWD/intake-state$SFX:/var/lib/fc-intake" -v "$SECRETS:/secrets:ro" -p 127.0.0.1:$((18400+PO)):8400 \
    -e INTAKE_STATE=/var/lib/fc-intake -e INTAKE_TEMPLATE=${INTAKE_TEMPLATE:-debian-13} -e INTAKE_LAYERS="$LAYERS" -e CP_URL=${CP_URL:-http://fc-cp$SFX:8700} ${CP_TOKEN:+-e CP_TOKEN=$CP_TOKEN} \
    -e MODEL_PROXY_URL=${MODEL_PROXY_URL:-http://fc-model$SFX:8500} -e GITEA_URL=${GITEA_URL:-http://fc-gitea:3000} ${GITEA_REPO:+-e GITEA_REPO=$GITEA_REPO} -e GITEA_TOKEN_FILE=${GITEA_TOKEN_FILE:-/secrets/gitea-cairn-bot.token} \
    -e GITEA_BOT=${GITEA_BOT:-cairn-bot} -e EVAL_URL=${EVAL_URL:-http://fc-eval$SFX:8600} -e MAX_PARALLEL=${MAX_PARALLEL:-2} \
    ${CHAT_WEBHOOK_URL:+-e CHAT_WEBHOOK_URL=$CHAT_WEBHOOK_URL} ${CHAT_FORMAT:+-e CHAT_FORMAT=$CHAT_FORMAT} ${CHAT_SECRET_FILE:+-e CHAT_SECRET_FILE=$CHAT_SECRET_FILE} ${CHAT_BOT:+-e CHAT_BOT=$CHAT_BOT} \
    ${CODE_HOST:+-e CODE_HOST=$CODE_HOST} ${GITHUB_REPO:+-e GITHUB_REPO=$GITHUB_REPO} ${GITHUB_TOKEN_FILE:+-e GITHUB_TOKEN_FILE=$GITHUB_TOKEN_FILE} ${GITHUB_BOT:+-e GITHUB_BOT=$GITHUB_BOT} ${ASSIGNMENT_TIMEOUT_S:+-e ASSIGNMENT_TIMEOUT_S=$ASSIGNMENT_TIMEOUT_S} ${INTAKE_MODE:+-e INTAKE_MODE=$INTAKE_MODE} \
    --entrypoint /usr/bin/python3 fc-node:dev /intake/intake.py >/dev/null
  exit 0
fi

N=${NODE:-}; NAME=fc-node${N:+-$N}; STATE=state${N:+-$N}; HPORT=$((18800 + ${N:-0}))
mkdir -p "$STATE"
docker stop "$NAME" >/dev/null 2>&1 || true      # SIGTERM: the agent pauses running persistent sandboxes to disk before exiting
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --hostname "$NAME" --restart unless-stopped --stop-timeout 120 \
  --device /dev/kvm --device /dev/net/tun --device /dev/vhost-vsock --device /dev/fuse \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add SYS_NICE --security-opt apparmor=unconfined --security-opt seccomp=unconfined \
  --cgroupns=private --sysctl net.ipv4.ip_forward=1 --network fcnet \
  -v "$PWD/images:/work/images:ro" -v "$PWD/recipes/templates:/work/templates:ro" -v "$PWD/$STATE:/var/lib/fc-node" -e TEMPLATES_DIR=/work/templates \
  -e NODE_NAME="$NAME" ${DEFAULT_TEMPLATE:+-e DEFAULT_TEMPLATE=$DEFAULT_TEMPLATE} -e MAX_VMS=${MAX_VMS:-32} -e MEM_BUDGET_MIB=${MEM_BUDGET_MIB:-8192} ${FENCE_AFTER_S:+-e FENCE_AFTER_S=$FENCE_AFTER_S} ${TLS_CERT:+-e TLS_CERT=$TLS_CERT -e TLS_KEY=$TLS_KEY} -e HOST_VCPUS=16 -e JAIL=${JAIL:-1} ${REGISTRY_MIRROR:+-e REGISTRY_MIRROR=$REGISTRY_MIRROR} \
  ${LAZY_STORE:+-e LAZY_STORE=$LAZY_STORE} ${STORE:+-e STORE=$STORE} ${TEMPLATE_POOL:+-e TEMPLATE_POOL=$TEMPLATE_POOL} -p 127.0.0.1:$HPORT:8800 \
  --entrypoint /usr/bin/python3 fc-node:dev /usr/local/bin/nodeagent.py >/dev/null
