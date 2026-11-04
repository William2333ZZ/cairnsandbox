FROM ubuntu:22.04
# Behind a regional mirror: --build-arg APT_MIRROR=mirrors.example.com --build-arg PIP_INDEX=https://mirror.example.com/pypi/simple/
ARG APT_MIRROR=
ARG PIP_INDEX=https://pypi.org/simple/
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s@//.*archive.ubuntu.com@//$APT_MIRROR@g; s@//security.ubuntu.com@//$APT_MIRROR@g" /etc/apt/sources.list; fi \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      squashfs-tools e2fsprogs iproute2 iptables nftables curl ca-certificates python3 python3-pip fuse3 libfuse2 socat file util-linux git \
 && pip3 install -q -i "$PIP_INDEX" fusepy \
 && rm -rf /var/lib/apt/lists/*
COPY bin/firecracker bin/jailer /usr/local/bin/
COPY node/*.sh node/*.py /usr/local/bin/
COPY node/guest /usr/local/bin/guest
RUN chmod +x /usr/local/bin/*.sh /usr/local/bin/*.py && echo user_allow_other >> /etc/fuse.conf
WORKDIR /work
ENTRYPOINT ["/bin/bash"]
