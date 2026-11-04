#!/usr/bin/env python3
"""Node-side networking for microVMs: one tap per VM on its own /30, default-deny, egress only via
the node's proxy; optional direct-IP allowlist per VM through nftables + masquerade.

Layout (inside the node container's netns):
  tap-<id>   host 10.200.x.y/30 (gateway for the guest), guest 10.200.x.y+1
  nft table inet fc: input  -> accept guest->gateway:PROXY_PORT, and replies to connections the node opened into the guest (port exposure, D39)
                     forward-> per-VM chain fwd_<id> (default drop)
"""
import hashlib, ipaddress, subprocess, threading

POOL = ipaddress.ip_network("10.200.0.0/16")
PROXY_PORT = 3128
_lock = threading.Lock()
_subnets = list(POOL.subnets(new_prefix=30))      # 16384 /30s
_used = set()


def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def mac_for(ip):
    b = ipaddress.ip_address(ip).packed
    return "02:fc:%02x:%02x:%02x:%02x" % tuple(b)


def ensure_base_rules():
    """Idempotent global ruleset."""
    script = f"""
table inet fc {{
  chain input {{
    type filter hook input priority 0; policy accept;
    iifname "tap-*" tcp dport {PROXY_PORT} accept
    iifname "tap-*" ct state established,related accept
    iifname "tap-*" udp dport 53 drop
    iifname "tap-*" drop
  }}
  chain forward {{
    type filter hook forward priority 0; policy drop;
    ct state established,related accept
  }}
  chain postrouting {{
    type nat hook postrouting priority 100; policy accept;
  }}
}}
"""
    subprocess.run(["nft", "flush", "table", "inet", "fc"], capture_output=True)
    subprocess.run(["nft", "delete", "table", "inet", "fc"], capture_output=True)
    subprocess.run(["nft", "-f", "-"], input=script, text=True, check=True)
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f: f.write("1")
    except OSError:                                   # /proc/sys is read-only in a container; use --sysctl
        if open("/proc/sys/net/ipv4/ip_forward").read().strip() != "1": raise RuntimeError("ip_forward is off; run with --sysctl net.ipv4.ip_forward=1")


class VMNet:
    def __init__(self, vm_id, owner=None):
        """owner=(uid, gid): the tap is created for that unprivileged user so a jailed VMM can attach without NET_ADMIN."""
        with _lock:
            sn = next(s for s in _subnets if s not in _used); _used.add(sn)
        self.subnet = sn; hosts = list(sn.hosts())
        self.gw, self.ip = str(hosts[0]), str(hosts[1]); self.prefix = 30
        h = hashlib.sha1(vm_id.encode()).hexdigest()[:10]          # ifname limit is 15 chars; ids can collide when truncated
        self.tap = f"tap-{h}"; self.mac = mac_for(self.ip); self.vm_id = vm_id
        self.chain = f"fwd_{h}"
        sh("ip", "tuntap", "add", "dev", self.tap, "mode", "tap", *(["user", str(owner[0]), "group", str(owner[1])] if owner else []))
        sh("ip", "addr", "add", f"{self.gw}/{self.prefix}", "dev", self.tap)
        sh("ip", "link", "set", self.tap, "up")
        sh("nft", "add", "chain", "inet", "fc", self.chain)
        sh("nft", "add", "rule", "inet", "fc", "forward", "iifname", self.tap, "jump", self.chain)
        self.direct = []

    def set_direct(self, cidrs):
        """Allow direct (non-proxy) egress to these CIDRs, masqueraded."""
        sh("nft", "flush", "chain", "inet", "fc", self.chain)
        for c in cidrs:
            sh("nft", "add", "rule", "inet", "fc", self.chain, "ip", "daddr", c, "accept")
        # masquerade for anything this VM is allowed to forward
        subprocess.run(["nft", "add", "rule", "inet", "fc", "postrouting", "ip", "saddr", self.ip, "masquerade"], capture_output=True)
        self.direct = list(cidrs)

    def guest_cfg(self):
        return {"ip": self.ip, "prefix": self.prefix, "gw": self.gw, "proxy": f"http://{self.gw}:{PROXY_PORT}"}

    def fc_iface(self):
        return {"iface_id": "eth0", "guest_mac": self.mac, "host_dev_name": self.tap}

    def destroy(self):
        subprocess.run(["ip", "link", "del", self.tap], capture_output=True)
        # drop the jump rule + chain
        out = subprocess.run(["nft", "-a", "list", "chain", "inet", "fc", "forward"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if f"jump {self.chain}" in line and "# handle" in line:
                subprocess.run(["nft", "delete", "rule", "inet", "fc", "forward", "handle", line.split("# handle")[1].strip()], capture_output=True)
        subprocess.run(["nft", "delete", "chain", "inet", "fc", self.chain], capture_output=True)
        with _lock: _used.discard(self.subnet)
