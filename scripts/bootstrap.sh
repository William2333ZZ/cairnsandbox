#!/bin/bash
# Everything a fresh Linux host needs before `docker compose up` (docs/010-deployment.md describes each step; this runs them in order).
#   scripts/bootstrap.sh            build everything not already built (safe to re-run; each step is skipped when its output exists)
#   scripts/bootstrap.sh --dry-run  say what each step would do on this machine, change nothing
#   scripts/bootstrap.sh --artifacts DIR   take the guest kernel, base image and empty writable layer from a release bundle (scripts/release-artifacts.sh makes one;
#                                          checked against its SHA256SUMS) instead of building them: skips the 10-40 minute kernel compile and the base image build
#   scripts/bootstrap.sh --skip-images     do not (re)build the node and control-plane Docker images (they already exist, or you load them from somewhere)
#   scripts/bootstrap.sh --skip-console    do not build the web console
# Firecracker and the kernel source are verified against the checksum file published next to them (set FC_SHA256 / KERNEL_SHA256 to pin a digest instead).
# Needs: Linux x86_64 with /dev/kvm, Docker 24+ with compose v2, curl, tar, python3, about 15 GB free and 30-60 minutes (the kernel build dominates).
# Behind a regional mirror, export before running:  APT_MIRROR=mirrors.example.com  PIP_INDEX=...  GOPROXY=https://goproxy.cn,direct
#   CARGO_MIRROR=sparse+https://rsproxy.cn/index/  KERNEL_URL=https://mirror.example.com/linux-6.18.59.tar.xz
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
. scripts/lib/verify.sh
FC_VER=${FC_VER:-v1.17.0}; KVER=${KVER:-6.18.59}
DRY=0; ART=""; SKIP_IMAGES=0; SKIP_CONSOLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1;; --skip-images) SKIP_IMAGES=1;; --skip-console) SKIP_CONSOLE=1;;
    --artifacts) shift; ART=${1:?--artifacts needs a directory};;
    *) echo "unknown option $1 (see the top of this script)" >&2; exit 2;;
  esac; shift
done
say() { printf '\n== %s\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
# run CMD... unless --dry-run, in which case just print it
do_() { if [ "$DRY" = 1 ]; then echo "   would run: $*"; else "$@"; fi; }
have() { [ -e "$1" ] && echo "present" || echo "missing"; }

say "0. checks"
need docker; need curl; need tar; need python3
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || { echo "Linux x86_64 only (the node runs Firecracker under KVM)" >&2; exit 1; }
[ -e /dev/kvm ] || { echo "/dev/kvm is missing: enable virtualization (on a cloud VM you need nested virtualization or a bare-metal instance)" >&2; exit 1; }
[ -r /dev/kvm ] && [ -w /dev/kvm ] || echo "warning: this user cannot open /dev/kvm; the node container gets the device from Docker, so this is fine if Docker can" >&2
docker compose version >/dev/null
if [ -n "$ART" ]; then
  [ -f "$ART/SHA256SUMS" ] || { echo "$ART has no SHA256SUMS: not a release bundle" >&2; exit 1; }
  echo "release bundle: $ART"; cat "$ART/manifest.json" 2>/dev/null | head -20
  ( cd "$ART" && if command -v sha256sum >/dev/null; then sha256sum -c SHA256SUMS; else shasum -a 256 -c SHA256SUMS; fi ) || { echo "the bundle does not match its SHA256SUMS" >&2; exit 1; }
fi

say "1. Firecracker $FC_VER ($(have bin/firecracker))"
if [ ! -x bin/firecracker ] || [ ! -x bin/jailer ]; then
  if [ "$DRY" = 1 ]; then echo "   would download firecracker-$FC_VER-x86_64.tgz from github.com/firecracker-microvm/firecracker/releases and verify it against the .sha256.txt published beside it"
  else
    mkdir -p bin; T=$(mktemp -d); A="firecracker-$FC_VER-x86_64.tgz"; U="https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER"
    fetch_verified "$U/$A" "$U/$A.sha256.txt" "$A" "$T/fc.tgz" "${FC_SHA256:-}"
    tar -C "$T" -xzf "$T/fc.tgz"
    cp "$T"/release-$FC_VER-x86_64/firecracker-$FC_VER-x86_64 bin/firecracker; cp "$T"/release-$FC_VER-x86_64/jailer-$FC_VER-x86_64 bin/jailer
    chmod +x bin/firecracker bin/jailer; rm -rf "$T"
  fi
fi
[ "$DRY" = 1 ] || bin/firecracker --version | head -1

say "2. images: node and control plane"
if [ "$SKIP_IMAGES" = 1 ]; then echo "   skipped (--skip-images): fc-node:dev and fc-cp:dev must already exist"
else do_ docker build -q -t fc-node:dev -f node/Dockerfile ${APT_MIRROR:+--build-arg APT_MIRROR=$APT_MIRROR} ${PIP_INDEX:+--build-arg PIP_INDEX=$PIP_INDEX} .
     DOCKER_BUILDKIT=1 do_ docker build -q -t fc-cp:dev ${GOPROXY:+--build-arg GOPROXY=$GOPROXY} cp/
fi

if [ -n "$ART" ]; then
  say "3-5b. kernel, base image and empty writable layer from the release bundle"
  mkdir -p images/kernel-out images/base images/layers images/bin-out
  K="vmlinux-$KVER-sbx"; [ -f "$ART/$K" ] || { echo "the bundle has no $K (it carries: $(ls "$ART" | tr '\n' ' '))" >&2; exit 1; }
  do_ cp "$ART/$K" "images/kernel-out/$K"; do_ cp "$ART/base-debian-13.ext4" images/base/base-debian-13.ext4; do_ cp "$ART/upper-template.ext4" images/layers/upper-template.ext4
  [ ! -f "$ART/sandboxd" ] || do_ cp "$ART/sandboxd" images/bin-out/sandboxd
else
say "3. guest kernel $KVER (about 10-40 minutes; $(have "images/kernel-out/vmlinux-$KVER-sbx"))"
if [ ! -f "images/kernel-out/vmlinux-$KVER-sbx" ]; then
  # the source tarball is checked against kernel.org's sha256sums.asc (or KERNEL_SHA256); a mirror via KERNEL_URL is checked the same way against the name
  if [ "$DRY" = 1 ]; then echo "   would build the kernel with recipes/kernel-6.18/build.sh (downloads linux-$KVER.tar.xz, verified)"
  else KERNEL_SHA256=${KERNEL_SHA256:-} recipes/kernel-6.18/build.sh images/kernel-out "$KVER"; fi
fi

say "4. guest runtime (Rust, static; $(have images/bin-out/sandboxd))"
[ -x images/bin-out/sandboxd ] || do_ recipes/sbx-runtime/build.sh images/bin-out

say "5. base image (Debian 13; $(have images/base/base-debian-13.ext4))"
[ -f images/base/base-debian-13.ext4 ] || do_ recipes/base-debian-13/build.sh images/base

say "5b. empty writable layer (copied into every new sandbox as its own disk)"
if [ ! -f images/layers/upper-template.ext4 ] && [ "$DRY" = 1 ]; then echo "   would create images/layers/upper-template.ext4 (512 MiB, ext4)"
elif [ ! -f images/layers/upper-template.ext4 ]; then
  mkdir -p images/layers
  docker run --rm -v "$PWD/images/layers:/out" --entrypoint bash fc-node:dev -c 'truncate -s 512M /out/upper-template.ext4 && mkfs.ext4 -q -F -L sbx-upper /out/upper-template.ext4'
fi
fi

say "6. configuration and secrets"
[ -f deploy/.env ] || do_ cp deploy/.env.example deploy/.env
if [ "$DRY" = 1 ]; then echo "   would run deploy/make-secrets.sh (creates secrets/cp.token and db.password if missing)"; else (cd deploy && ./make-secrets.sh); fi
if [ "$SKIP_CONSOLE" = 1 ]; then echo "   console skipped (--skip-console)"; else [ -d console-dist ] || do_ console/build.sh; fi

say "7. publish kernel and base to the chunk store (nodes read images from it)"
do_ mkdir -p deploy/store
do_ python3 node/imgstore.py push deploy/store "vmlinux-$KVER-sbx" "images/kernel-out/vmlinux-$KVER-sbx"
do_ python3 node/imgstore.py push deploy/store base-debian-13.ext4 images/base/base-debian-13.ext4

cat <<MSG

Done. Next:
  cd deploy && docker compose up -d
  curl -s -H "Authorization: Bearer \$(cat secrets/cp.token)" http://127.0.0.1:18700/health
  pip install -e ../sdk/python && CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=\$(cat secrets/cp.token) python3 ../sdk/python/examples/quickstart.py
MSG
