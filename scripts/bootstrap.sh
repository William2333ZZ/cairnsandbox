#!/bin/bash
# Everything a fresh Linux host needs before `docker compose up` (docs/010-deployment.md describes each step; this runs them in order).
#   scripts/bootstrap.sh            build everything not already built (safe to re-run; each step is skipped when its output exists)
# Needs: Linux x86_64 with /dev/kvm, Docker 24+ with compose v2, curl, tar, python3, about 15 GB free and 30-60 minutes (the kernel build dominates).
# Behind a regional mirror, export before running:  APT_MIRROR=mirrors.example.com  PIP_INDEX=...  GOPROXY=https://goproxy.cn,direct
#   CARGO_MIRROR=sparse+https://rsproxy.cn/index/  KERNEL_URL=https://mirror.example.com/linux-6.18.59.tar.xz
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
FC_VER=${FC_VER:-v1.17.0}; KVER=${KVER:-6.18.59}
say() { printf '\n== %s\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }

say "0. checks"
need docker; need curl; need tar; need python3
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || { echo "Linux x86_64 only (the node runs Firecracker under KVM)" >&2; exit 1; }
[ -e /dev/kvm ] || { echo "/dev/kvm is missing: enable virtualization (on a cloud VM you need nested virtualization or a bare-metal instance)" >&2; exit 1; }
[ -r /dev/kvm ] && [ -w /dev/kvm ] || echo "warning: this user cannot open /dev/kvm; the node container gets the device from Docker, so this is fine if Docker can" >&2
docker compose version >/dev/null

say "1. Firecracker $FC_VER"
if [ ! -x bin/firecracker ] || [ ! -x bin/jailer ]; then
  mkdir -p bin; T=$(mktemp -d)
  curl -fSL -o "$T/fc.tgz" "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER/firecracker-$FC_VER-x86_64.tgz"
  tar -C "$T" -xzf "$T/fc.tgz"
  cp "$T"/release-$FC_VER-x86_64/firecracker-$FC_VER-x86_64 bin/firecracker; cp "$T"/release-$FC_VER-x86_64/jailer-$FC_VER-x86_64 bin/jailer
  chmod +x bin/firecracker bin/jailer; rm -rf "$T"
fi
bin/firecracker --version | head -1

say "2. images: node and control plane"
docker build -q -t fc-node:dev -f node/Dockerfile ${APT_MIRROR:+--build-arg APT_MIRROR=$APT_MIRROR} ${PIP_INDEX:+--build-arg PIP_INDEX=$PIP_INDEX} .
DOCKER_BUILDKIT=1 docker build -q -t fc-cp:dev ${GOPROXY:+--build-arg GOPROXY=$GOPROXY} cp/

say "3. guest kernel $KVER (about 10-40 minutes)"
[ -f "images/kernel-out/vmlinux-$KVER-sbx" ] || recipes/kernel-6.18/build.sh images/kernel-out "$KVER"

say "4. guest runtime (Rust, static)"
[ -x images/bin-out/sandboxd ] || recipes/sbx-runtime/build.sh images/bin-out

say "5. base image (Debian 13)"
[ -f images/base/base-debian-13.ext4 ] || recipes/base-debian-13/build.sh images/base

say "5b. empty writable layer (copied into every new sandbox as its own disk)"
if [ ! -f images/layers/upper-template.ext4 ]; then
  mkdir -p images/layers
  docker run --rm -v "$PWD/images/layers:/out" --entrypoint bash fc-node:dev -c 'truncate -s 512M /out/upper-template.ext4 && mkfs.ext4 -q -F -L sbx-upper /out/upper-template.ext4'
fi

say "6. configuration and secrets"
[ -f deploy/.env ] || cp deploy/.env.example deploy/.env
(cd deploy && ./make-secrets.sh)
[ -d console-dist ] || console/build.sh

say "7. publish kernel and base to the chunk store (nodes read images from it)"
mkdir -p deploy/store
python3 node/imgstore.py push deploy/store "vmlinux-$KVER-sbx" "images/kernel-out/vmlinux-$KVER-sbx"
python3 node/imgstore.py push deploy/store base-debian-13.ext4 images/base/base-debian-13.ext4

cat <<MSG

Done. Next:
  cd deploy && docker compose up -d
  curl -s -H "Authorization: Bearer \$(cat secrets/cp.token)" http://127.0.0.1:18700/health
  pip install -e ../sdk/python && CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=\$(cat secrets/cp.token) python3 ../sdk/python/examples/quickstart.py
MSG
