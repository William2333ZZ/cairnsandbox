#!/bin/bash
# usage: smoke-boot.sh <vmlinux> <rootfs.ext4>
set -euo pipefail
KERNEL=$1; ROOTFS=$2
ID=smoke-$$
WD=/tmp/$ID; mkdir -p "$WD"
cp --reflink=auto "$ROOTFS" "$WD/rootfs.ext4"; ROOTFS="$WD/rootfs.ext4"
cat > "$WD/config.json" <<JSON
{
  "boot-source": {
    "kernel_image_path": "$KERNEL",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/fc-smoke-init"
  },
  "drives": [{ "drive_id": "rootfs", "path_on_host": "$ROOTFS", "is_root_device": true, "is_read_only": false }],
  "machine-config": { "vcpu_count": 2, "mem_size_mib": 512 }
}
JSON
START=$(date +%s.%N)
timeout 60 firecracker --no-api --config-file "$WD/config.json" > "$WD/serial.log" 2>&1 || true
END=$(date +%s.%N)
echo "--- serial tail ---"; tail -n 8 "$WD/serial.log"
echo "--- elapsed: $(echo "$END - $START" | bc 2>/dev/null || python3 -c "print(round($END-$START,2))")s"
grep -q FC_SMOKE_OK "$WD/serial.log" && echo "SMOKE: PASS" || { echo "SMOKE: FAIL"; exit 1; }
