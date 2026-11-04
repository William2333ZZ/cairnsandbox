#!/bin/bash
# env layer "go-toolchain": Go 1.26 under /usr/local/go, a pre-warmed module cache under /opt/gomod (read-only use),
# PostgreSQL from the apt mirror. Tarballs are staged in /tmp/recipe-files (the box cannot reach go.dev / proxy.golang.org).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
F=/tmp/recipe-files
mkdir -p /usr/local /opt
tar -C /usr/local -xzf "$F/go.tar.gz"
tar -C /opt -xzf "$F/modcache.tar.gz" && mv /opt/modcache /opt/gomod
ln -sf /usr/local/go/bin/go /usr/local/bin/go; ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
cat > /etc/profile.d/go.sh <<'EOP'
export GOMODCACHE=/opt/gomod GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local GOCACHE=/tmp/gocache
EOP
chmod 0644 /etc/profile.d/go.sh
mkdir -p /var/cache/apt/archives/partial /var/lib/apt/lists/partial /var/lib/dpkg/updates /var/log/apt /var/log/dpkg
if [ -n "${APT_MIRROR:-}" ]; then for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do   # build-layer.sh passes APT_MIRROR through
  [ -f "$f" ] && sed -i "s#http://deb.debian.org/debian-security#http://$APT_MIRROR/debian-security#;s#http://deb.debian.org/debian#http://$APT_MIRROR/debian#" "$f"
done; fi
apt-get -qq update
apt-get -qq install -y --no-install-recommends postgresql >/dev/null
apt-get clean; rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /var/cache/apt/*
chmod -R a+rX /opt/gomod /usr/local/go
. /etc/profile.d/go.sh; /usr/local/go/bin/go version; ls /usr/lib/postgresql; du -sh /usr/local/go /opt/gomod /usr/lib/postgresql | tr "\n" " "; echo
