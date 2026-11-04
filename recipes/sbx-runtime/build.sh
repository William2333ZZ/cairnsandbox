#!/bin/bash
# Build the guest runtime (Rust) in a throwaway rust:1-slim-trixie container (same glibc as the Debian 13 base);
# crates from crates.io (set CARGO_MIRROR=sparse+https://rsproxy.cn/index/ behind a regional mirror). usage: build.sh <out-dir>  -> <out-dir>/sandboxd (static, stripped) + manifest
set -euo pipefail
OUT=$(readlink -f "$1"); HERE=$(dirname "$(readlink -f "$0")"); SRC=$(readlink -f "$HERE/../../guest/sbx-runtime"); mkdir -p "$OUT" "$OUT/.cargo-cache"
docker run --rm -e CARGO_MIRROR="${CARGO_MIRROR:-}" -v "$SRC:/src:ro" -v "$OUT:/out" -v "$OUT/.cargo-cache:/usr/local/cargo/registry" rust:1-slim-trixie bash -c '
set -euo pipefail
mkdir -p ~/.cargo && if [ -n "$CARGO_MIRROR" ]; then cat > ~/.cargo/config.toml <<CFG
[source.crates-io]
replace-with = "mirror"
[source.mirror]
registry = "$CARGO_MIRROR"
[net]
git-fetch-with-cli = true
CFG
fi
cp -r /src /tmp/b && cd /tmp/b
RUSTFLAGS="-C target-feature=+crt-static" cargo build --release --target x86_64-unknown-linux-gnu 2>&1 | tail -5
cp target/x86_64-unknown-linux-gnu/release/sbx-runtime /out/sandboxd && ls -la --block-size=K /out/sandboxd && file /out/sandboxd 2>/dev/null || true
sha256sum /out/sandboxd'
