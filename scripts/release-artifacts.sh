#!/bin/bash
# Package the guest artifacts of a working checkout as a release bundle that `scripts/bootstrap.sh --artifacts DIR` can consume, so a new machine skips the
# kernel compile (10-40 min) and the base image build.
#   scripts/release-artifacts.sh [out-dir]        (default: release-out/cairn-artifacts-<kver>-<date>)
# Takes from images/: kernel-out/vmlinux-<kver>-sbx (+ its .config), base/base-debian-13.ext4, layers/upper-template.ext4, bin-out/sandboxd.
# Writes: those files, manifest.json (versions, git commit, sizes), SHA256SUMS, and SOURCE-OFFER.md (the kernel is GPL-2.0: where its source and our config are).
# It does NOT upload anything: attaching the bundle to a release is a separate, deliberate step (the kernel's licence, and what the base image contains, are yours to check first).
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
KVER=${KVER:-6.18.59}; STAMP=$(date -u +%Y%m%d); OUT=${1:-release-out/cairn-artifacts-$KVER-$STAMP}
K="images/kernel-out/vmlinux-$KVER-sbx"
for f in "$K" images/base/base-debian-13.ext4 images/layers/upper-template.ext4; do [ -f "$f" ] || { echo "missing $f: build it first (scripts/bootstrap.sh)" >&2; exit 1; }; done
mkdir -p "$OUT"
cp --sparse=always "$K" "$OUT/"; cp --sparse=always images/base/base-debian-13.ext4 images/layers/upper-template.ext4 "$OUT/"
[ ! -f "$K.config" ] || cp "$K.config" "$OUT/"; [ ! -x images/bin-out/sandboxd ] || cp images/bin-out/sandboxd "$OUT/"
COMMIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)
cat > "$OUT/SOURCE-OFFER.md" <<EOF
# Source for the GPL-licensed parts of this bundle

- \`vmlinux-$KVER-sbx\` is the Linux kernel $KVER (GPL-2.0), built from the unmodified source tarball
  https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KVER.tar.xz with the configuration recipes/kernel-6.18/firecracker-microvm-6.18.config plus the option fragment
  recipes/kernel-6.18/trim.fragment (repository commit $COMMIT)$( [ -f "$K.config" ] && echo "; the resulting .config is included as vmlinux-$KVER-sbx.config" ).
  Anyone may rebuild it: \`recipes/kernel-6.18/build.sh images/kernel-out $KVER\`.
- \`base-debian-13.ext4\` is a Debian 13 root filesystem plus our init and runtime; the packages in it are under their own licences, their sources are available from Debian.
  The recipe is recipes/base-debian-13/.
EOF
python3 - "$OUT" "$KVER" "$COMMIT" <<'PY'
import json, os, sys, time
out, kver, commit = sys.argv[1:4]
files = {f: {"bytes": os.path.getsize(os.path.join(out, f))} for f in sorted(os.listdir(out)) if f not in ("manifest.json", "SHA256SUMS")}
json.dump({"kind": "cairn-artifacts", "kernel": kver, "commit": commit, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": files,
           "use": "scripts/bootstrap.sh --artifacts <this directory>"}, open(os.path.join(out, "manifest.json"), "w"), indent=1)
PY
( cd "$OUT" && sha256sum $(ls | grep -v '^SHA256SUMS$') > SHA256SUMS )
echo "bundle: $OUT"; ls -la --block-size=M "$OUT" | tail -n +2; du -sh --apparent-size "$OUT" | cut -f1 | sed 's/^/apparent size: /'
