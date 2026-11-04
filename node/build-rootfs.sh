#!/bin/bash
# usage: build-rootfs.sh <squashfs> <out-dir> [base-size] [upper-size]
# Produces <out-dir>/base.ext4 (read-only base layer: OS + fc-init + sandboxd + private shell +
# unprivileged `agent` user) and <out-dir>/upper-template.ext4 (empty writable layer, sparse).
set -euo pipefail
SQ=$1; OUT=$2; BSIZE=${3:-1G}; USIZE=${4:-512M}
HERE=$(dirname "$(readlink -f "$0")")
R=/tmp/sqroot
rm -rf $R && unsquashfs -q -d $R "$SQ" >/dev/null

# mount points fc-init needs (the base is mounted read-only, so they must pre-exist)
mkdir -p $R/mnt $R/proc $R/sys $R/dev $R/run $R/tmp

# daemon + init + private shell copy, all root-owned inside the read-only layer
install -D -m 0755 "$HERE/guest/fc-init"     $R/sbin/fc-init
install -D -m 0755 "$HERE/guest/sandboxd.py" $R/usr/local/lib/sandboxd/sandboxd.py
install -D -m 0755 $R/bin/bash               $R/usr/local/lib/sandboxd/bash
chmod 0755 $R/usr/local/lib/sandboxd   # root-owned, read-only layer: executable by agent, writable by nobody

# smoke init kept for the kernel-only test
install -m 0755 /dev/stdin $R/sbin/fc-smoke-init <<'INIT'
#!/bin/sh
mount -t proc proc /proc 2>/dev/null; mount -t sysfs sys /sys 2>/dev/null
echo "FC_SMOKE_OK kernel=$(uname -r) cpus=$(nproc) mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)"
sync; echo o > /proc/sysrq-trigger; sleep 5
INIT

# unprivileged task user
if ! grep -q '^agent:' $R/etc/passwd; then
  chroot $R /usr/sbin/useradd -m -u 2000 -U -s /usr/local/lib/sandboxd/bash agent 2>/dev/null \
   || { echo 'agent:x:2000:2000::/home/agent:/bin/bash' >> $R/etc/passwd; echo 'agent:x:2000:' >> $R/etc/group;
        mkdir -p $R/home/agent; }
fi
# login shell for the agent user
sed -i 's#^agent:\(.*\):/usr/local/lib/sandboxd/bash$#agent:\1:/bin/bash#' $R/etc/passwd
chown -R 2000:2000 $R/home/agent
chmod 1777 $R/tmp
chown -R root:root $R/sbin $R/usr/local/lib/sandboxd

mkdir -p "$OUT"
truncate -s "$BSIZE" "$OUT/base.ext4"
mkfs.ext4 -q -F -L sbx-base -d $R "$OUT/base.ext4"
e2fsck -fn "$OUT/base.ext4" >/dev/null && echo "base ok: $OUT/base.ext4 ($(du -h "$OUT/base.ext4" | cut -f1) used)"

rm -f "$OUT/upper-template.ext4"
truncate -s "$USIZE" "$OUT/upper-template.ext4"
mkfs.ext4 -q -F -L sbx-upper "$OUT/upper-template.ext4"
echo "upper template ok: $OUT/upper-template.ext4 (apparent $USIZE, allocated $(du -h "$OUT/upper-template.ext4" | cut -f1))"
