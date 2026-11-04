#!/bin/bash
# env layer "dev-tools": git, C toolchain, make, jq, python3 + pytest (what the tasks need). The harness lives in the agent-harness layer.
# Runs as root inside a build sandbox (network: apt mirror only); recipe files are under /tmp/recipe-files.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/cache/apt/archives/partial /var/lib/apt/lists/partial /var/lib/dpkg/updates /var/log/apt /var/log/dpkg; chmod 1777 /tmp
if [ -n "${APT_MIRROR:-}" ]; then for f in /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do   # build-layer.sh passes APT_MIRROR through
  [ -f "$f" ] && sed -i "s#http://archive.ubuntu.com/ubuntu/#http://$APT_MIRROR/ubuntu/#;s#http://security.ubuntu.com/ubuntu/#http://$APT_MIRROR/ubuntu/#;s#http://deb.debian.org/debian-security#http://$APT_MIRROR/debian-security#;s#http://deb.debian.org/debian#http://$APT_MIRROR/debian#" "$f"
done; fi
apt-get -qq update
apt-get -qq install -y --no-install-recommends git gcc libc6-dev make jq python3 python3-pytest python-is-python3 ca-certificates >/dev/null
apt-get clean; rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /var/log/apt/* /var/cache/apt/*
git config --system user.email agent@sandbox; git config --system user.name agent; git config --system init.defaultBranch main
cd /tmp && python3 -c "import pytest; print('selfcheck ok')" && python --version
git --version; gcc --version | head -1; python3 --version
