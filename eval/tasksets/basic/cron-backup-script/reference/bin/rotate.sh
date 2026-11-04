#!/usr/bin/env bash
# rotate.sh SRC DEST KEEP: archive SRC into DEST/backup-NNNN.tar.gz and keep only the newest KEEP archives.
set -euo pipefail
if [ "$#" -ne 3 ]; then echo "usage: $0 SRC DEST KEEP" >&2; exit 2; fi
SRC="$1"; DEST="$2"; KEEP="$3"
[ -d "$SRC" ] || { echo "error: SRC '$SRC' is not a directory" >&2; exit 3; }
case "$KEEP" in ''|*[!0-9]*) echo "error: KEEP must be a positive integer" >&2; exit 4;; esac
[ "$KEEP" -ge 1 ] || { echo "error: KEEP must be a positive integer" >&2; exit 4; }
mkdir -p "$DEST"
last=$(ls "$DEST" 2>/dev/null | sed -n 's/^backup-\([0-9][0-9][0-9][0-9]\)\.tar\.gz$/\1/p' | sort | tail -1)
next=$(printf '%04d' $((10#${last:-0} + 1)))
out="$DEST/backup-$next.tar.gz"
tar -C "$SRC" -czf "$out" .
ls "$DEST" | grep -E '^backup-[0-9]{4}\.tar\.gz$' | sort | head -n -"$KEEP" 2>/dev/null | while read -r f; do rm -f "$DEST/$f"; done || true
# head -n -K is GNU-only; portable fallback:
n=$(ls "$DEST" | grep -cE '^backup-[0-9]{4}\.tar\.gz$' || true)
while [ "$n" -gt "$KEEP" ]; do
  oldest=$(ls "$DEST" | grep -E '^backup-[0-9]{4}\.tar\.gz$' | sort | head -1)
  rm -f "$DEST/$oldest"; n=$((n - 1))
done
echo "$out"
