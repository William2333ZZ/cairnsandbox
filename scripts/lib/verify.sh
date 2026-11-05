#!/bin/bash
# Download-and-verify helpers for scripts/bootstrap.sh and recipes. Sourced, not run.
#
#   sha256_of FILE                         -> the hex digest (sha256sum or shasum)
#   verify_sha256 FILE EXPECTED_HEX        -> 0 if equal, 1 (with a message) if not
#   expected_from_sums SUMS_FILE NAME      -> the digest listed for NAME in a `sha256sum`-style file (either "<hex>  NAME" or "<hex> *NAME")
#   fetch_verified URL SUMS_URL NAME OUT [PINNED_HEX]
#        download URL to OUT and check it against PINNED_HEX if given, else against the line for NAME in the file at SUMS_URL (fetched from the same place;
#        that guards against a corrupted or truncated download, not against a compromised host: pin the digest for that).
# URLs may be https:// or file:// (the tests use file://).
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

verify_sha256() {
  local f=$1 want=$2 got; got=$(sha256_of "$f")
  [ "$got" = "$want" ] && return 0
  echo "checksum mismatch for $f: expected $want, got $got" >&2; return 1
}

expected_from_sums() {
  awk -v n="$2" '{ f=$2; sub(/^\*/, "", f); if (f == n) { print $1; exit } }' "$1"
}

fetch_verified() {
  local url=$1 sums=$2 name=$3 out=$4 pinned=${5:-} want tmp
  curl -fSL --retry 3 -o "$out" "$url" || { echo "download failed: $url" >&2; return 1; }
  if [ -n "$pinned" ]; then want=$pinned
  else
    tmp=$(mktemp); curl -fSL --retry 3 -o "$tmp" "$sums" || { rm -f "$tmp" "$out"; echo "could not fetch the checksum file: $sums" >&2; return 1; }
    want=$(expected_from_sums "$tmp" "$name"); rm -f "$tmp"
    [ -n "$want" ] || { rm -f "$out"; echo "no checksum for $name in $sums" >&2; return 1; }
  fi
  verify_sha256 "$out" "$want" || { rm -f "$out"; return 1; }
}
