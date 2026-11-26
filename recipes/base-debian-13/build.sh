#!/bin/bash
# Build the base layer from the Dockerfile recipe: OCI build -> export -> add fc-init/sandboxd/agent user -> ext4.
# usage: build.sh <out-dir> [base-size]   -> <out-dir>/base-debian-13.ext4 (label sbx-base) + manifest json
set -euo pipefail
mkdir -p "$1"
OUT=$(readlink -f "$1"); BSIZE=${2:-1536M}; HERE=$(dirname "$(readlink -f "$0")"); REPO=$(readlink -f "$HERE/../..")
T3=$(realpath "${T3_BUNDLE_DIR:?set T3_BUNDLE_DIR to the clean unpacked linux-x64 T3 release directory}")
test -x "$T3/t3" || { echo 'T3 bundle lacks executable t3' >&2; exit 1; }
mkdir -p "$OUT"; TAG=fc-base-debian-13:build
docker build -q --build-arg APT_MIRROR="${APT_MIRROR:-}" -t "$TAG" "$HERE" | tail -1
C=$(docker create "$TAG" /bin/true); docker export "$C" -o "$OUT/rootfs.tar"; docker rm "$C" >/dev/null
BIN=${SANDBOXD_BIN:-$REPO/images/bin-out/sandboxd}; test -x "$BIN" || { echo "need the Rust runtime at $BIN (recipes/sbx-runtime/build.sh)"; exit 1; }
docker run --rm -v "$OUT:/out" -v "$REPO/node/guest:/guest:ro" -v "$T3:/t3-release:ro" -v "$BIN:/sandboxd-bin:ro" -e BSIZE="$BSIZE" --entrypoint /bin/bash fc-node:dev -c '
set -euo pipefail
R=/tmp/r; rm -rf $R; mkdir -p $R; tar -C $R -xf /out/rootfs.tar
# docker leaves /etc/hosts empty in an export, so nothing resolved "localhost" (Postgres, anything listening on it, failed to start): D75
printf "127.0.0.1 localhost\n::1 localhost ip6-localhost\n" > $R/etc/hosts
rm -f $R/.dockerenv; mkdir -p $R/mnt $R/proc $R/sys $R/dev $R/run $R/tmp
install -D -m 0755 /guest/fc-init     $R/sbin/fc-init
install -D -m 0755 /sandboxd-bin        $R/usr/local/lib/sandboxd/sandboxd
install -D -m 0755 $R/bin/bash        $R/usr/local/lib/sandboxd/bash
chmod 0755 $R/usr/local/lib/sandboxd
grep -q "^agent:" $R/etc/passwd || { echo "agent:x:2000:2000::/home/agent:/bin/bash" >> $R/etc/passwd; echo "agent:x:2000:" >> $R/etc/group; echo "agent:!:20000:0:99999:7:::" >> $R/etc/shadow; }
grep -q "^aios:" $R/etc/passwd || { echo "aios:x:2001:2001::/var/lib/aios:/usr/sbin/nologin" >> $R/etc/passwd; echo "aios:x:2001:" >> $R/etc/group; }
mkdir -p $R/home/agent $R/var/lib/aios; chown -R 2000:2000 $R/home/agent; chown -R 2001:2001 $R/var/lib/aios; chmod 1777 $R/tmp
mkdir -p $R/opt/t3
cp -a /t3-release/. $R/opt/t3/
chown -Rh root:root $R/opt/t3
chmod -R go-w $R/opt/t3
ln -s /opt/t3 $R/home/agent/t3-fork
install -m 0755 /guest/start-t3 $R/usr/local/lib/sandboxd/start-t3
chroot --userspec=2000:2000 $R /opt/t3/t3 --version
chown -R root:root $R/sbin $R/usr/local/lib/sandboxd
FILES=$(find $R -xdev -type f | wc -l); USED=$(du -sm $R | cut -f1)
rm -f /out/base-debian-13.ext4; truncate -s "$BSIZE" /out/base-debian-13.ext4
mkfs.ext4 -q -F -L sbx-base -d $R /out/base-debian-13.ext4
e2fsck -fn /out/base-debian-13.ext4 >/dev/null
python3 - <<PY
import json,hashlib,os,time
p="/out/base-debian-13.ext4"; h=hashlib.sha256()
with open(p,"rb") as f:
    for b in iter(lambda: f.read(1<<20), b""): h.update(b)
json.dump({"layer":"base-debian-13","role":"L0+L1","sandboxd":"rust","t3_preinstalled":True,"files":$FILES,"used_mib":$USED,"ext4_bytes":os.path.getsize(p),"sha256":h.hexdigest(),"built":time.time(),
           "recipe":"recipes/base-debian-13/Dockerfile"},open("/out/base-debian-13.json","w"),indent=1)
print("base ok: files=$FILES used=${USED}MiB")
PY'
rm -f "$OUT/rootfs.tar"; cat "$OUT/base-debian-13.json"
