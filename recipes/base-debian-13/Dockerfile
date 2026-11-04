# L0+L1 base: Debian 13 essential + exactly what fc-init and the Rust sandboxd need. No python, no systemd, no docs, no locales.
# (docs/003-guest.md). The daemon, init and users are added by build.sh after export so this stays a plain OCI build.
FROM debian:trixie-slim
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s#http://deb.debian.org/debian-security#http://$APT_MIRROR/debian-security#;s#http://deb.debian.org/debian#http://$APT_MIRROR/debian#" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get -qq update \
 && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends \
      bash util-linux mount e2fsprogs iproute2 tar hostname ca-certificates curl \
 && apt-get clean && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/locale /usr/share/info \
      /usr/share/lintian /var/cache/apt/* /var/log/* \
 && mkdir -p /mnt /proc /sys /dev /run /tmp && chmod 1777 /tmp
