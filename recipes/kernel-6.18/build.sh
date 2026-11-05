#!/bin/bash
# Build the trimmed guest kernel in a throwaway container (no host packages needed).
# usage: build.sh <out-dir> [version]   -> <out-dir>/vmlinux-<version>-sbx, .config, build.log; prints size + key options
set -euo pipefail
OUT=$(readlink -f "$1"); VER=${2:-6.18.59}; HERE=$(dirname "$(readlink -f "$0")"); mkdir -p "$OUT"
docker run --rm -v "$HERE:/recipe:ro" -v "$OUT:/out" -e VER="$VER" -e APT_MIRROR="${APT_MIRROR:-}" -e KERNEL_SHA256="${KERNEL_SHA256:-}" -e KERNEL_URL="${KERNEL_URL:-https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$VER.tar.xz}" -e JOBS="${JOBS:-16}" ubuntu:22.04 bash -c '
set -euo pipefail
if [ -n "$APT_MIRROR" ]; then sed -i "s#http://archive.ubuntu.com/ubuntu/#http://$APT_MIRROR/ubuntu/#;s#http://security.ubuntu.com/ubuntu/#http://$APT_MIRROR/ubuntu/#" /etc/apt/sources.list; fi
apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends build-essential flex bison libelf-dev libssl-dev bc kmod cpio xz-utils python3 curl ca-certificates >/dev/null
cd /tmp && curl -sfSL -o k.tar.xz "$KERNEL_URL"
# verify the tarball: against KERNEL_SHA256 when given, else against the line for linux-$VER.tar.xz in sha256sums.asc next to the default download location
if [ -n "${KERNEL_SHA256:-}" ]; then want="$KERNEL_SHA256"
else curl -sfSL -o sums.asc "https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc" && want=$(awk -v n="linux-$VER.tar.xz" "\$2 == n {print \$1; exit}" sums.asc) || want=""; fi
[ -n "$want" ] || { echo "cannot verify the kernel tarball: no KERNEL_SHA256 and no entry for linux-$VER.tar.xz in sha256sums.asc" >&2; exit 1; }
[ "$(sha256sum k.tar.xz | cut -d" " -f1)" = "$want" ] || { echo "kernel tarball checksum mismatch (expected $want)" >&2; exit 1; }
echo "kernel tarball verified: $want"
tar -xf k.tar.xz && cd "linux-$VER"
cp /recipe/firecracker-microvm-6.18.config .config
# apply the fragment with scripts/config so dependencies are resolved by olddefconfig afterwards
grep -vE "^\s*(#|$)" /recipe/trim.fragment | while IFS= read -r line; do
  k=${line%%=*}; v=${line#*=}
  case "$v" in n) scripts/config --disable "$k";; y) scripts/config --enable "$k";; *) scripts/config --set-str "$k" "$(echo "$v" | tr -d \")";; esac
done
make -s olddefconfig
echo "== effective options"; grep -E "^CONFIG_(MODULES|IO_URING|USER_NS|BPF_SYSCALL|NETFILTER|IPV6|SECURITY_LANDLOCK|LSM|EROFS_FS|FS_DAX|VIRTIO_PMEM|VIRTIO_BALLOON|PAGE_REPORTING|VSOCKETS|OVERLAY_FS|SECCOMP_FILTER|FANOTIFY|DEBUG_INFO_NONE|KALLSYMS)=" .config || true
echo "== missing (expected =y)"; for k in SECURITY_LANDLOCK EROFS_FS FS_DAX VIRTIO_PMEM VIRTIO_BALLOON VSOCKETS OVERLAY_FS SECCOMP_FILTER; do grep -q "^CONFIG_$k=y" .config || echo "  CONFIG_$k"; done
echo "== =y count: $(grep -c "=y$" .config)"
t0=$(date +%s); make -s -j"$JOBS" vmlinux > /out/build.log 2>&1; echo "== built in $(( $(date +%s)-t0 )) s"
cp vmlinux "/out/vmlinux-$VER-sbx"; cp .config "/out/vmlinux-$VER-sbx.config"; ls -la --block-size=M "/out/vmlinux-$VER-sbx"
'
