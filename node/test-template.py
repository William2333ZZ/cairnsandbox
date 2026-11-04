#!/usr/bin/env python3
"""Verify a guest template (kernel + base layer): boot, isolation features of the trimmed kernel, users, overlay root,
network through the proxy, pack, template snapshot; prints boot time and lazily-fetched bytes.
usage: test-template.py <node-url> <template> [compare-template]"""
import atexit, json, os, sys, time, urllib.request

N, TPL = sys.argv[1].rstrip("/"), sys.argv[2]; CMP = sys.argv[3] if len(sys.argv) > 3 else None
TOK = os.environ.get("NODE_TOKEN", "")
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))

def req(method, path, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(N + path, data=data, method=method, headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOK}"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp: return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:300].decode(errors="replace")})

def ex(sid, cmd, user="agent", timeout=60):
    c, r = req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, "user": user, "timeout": timeout}); return r

def lazy_bytes():
    c, s = req("GET", "/lazy/stats"); return (s.get("fetched_bytes") or s.get("bytes_fetched") or 0) if c == 200 else None

def boot(tpl, **spec):
    lb0 = lazy_bytes(); t0 = time.monotonic()
    c, sb = req("POST", "/sandboxes", {"template": tpl, "class": "ephemeral", "mem": 512, "vcpu": 1, "boot": "cold", "ttl_idle_s": 300, **spec})
    dt = time.monotonic() - t0
    if c != 201: return None, dt, sb, None
    r = ex(sb["id"], "true"); lb1 = lazy_bytes()
    return sb, dt, r, (lb1 - lb0) if (lb0 is not None and lb1 is not None) else None

print(f"== template {TPL} ==")
sb, dt, r, fetched = boot(TPL)
step("cold boot + first exec", sb is not None and r.get("exit") == 0, f"{dt:.2f}s create→ready; lazily fetched {fetched and fetched // 1048576} MiB; {'' if sb else r}")
if not sb: sys.exit(0)
S = sb["id"]
r = ex(S, "uname -r; cat /etc/os-release | grep ^PRETTY; findmnt -n -o FSTYPE /; id; command -v python3 >/dev/null && python3 --version || echo no-python3; cat /proc/sys/kernel/random/boot_id >/dev/null && echo rng-ok", "root")
out = r.get("stdout", ""); print("   " + out.strip().replace("\n", " | "))
step("overlay root, agent user", "overlay" in out and "uid=2000" in ex(S, "id").get("stdout", ""))
r = ex(S, "grep -c erofs /proc/filesystems; grep -c overlay /proc/filesystems; cat /proc/sys/kernel/modules_disabled 2>/dev/null || echo no-modules-sysctl; ls /lib/modules 2>/dev/null | wc -l", "root")
o = r.get("stdout", "").split()
step("erofs + overlay in kernel, no modules", len(o) >= 4 and o[0] == "1" and o[1] == "1" and o[2] == "no-modules-sysctl" and o[3] == "0", " ".join(o))
# shell-only probes (the base has no python3/curl): securityfs lists the active LSMs; the io_uring sysctl only exists when it is compiled in
r = ex(S, "mountpoint -q /sys/kernel/security || mount -t securityfs none /sys/kernel/security; cat /sys/kernel/security/lsm; echo; test -e /proc/sys/kernel/io_uring_disabled && echo io_uring-present || echo io_uring-absent; unshare -U true 2>&1 | head -1; echo userns-rc=${PIPESTATUS[0]}", "root")
o = r.get("stdout", ""); print("   " + o.strip().replace("\n", " | "))
step("landlock is an active LSM", "landlock" in o.splitlines()[0] if o else False, o.splitlines()[0] if o else "")
step("io_uring compiled out", "io_uring-absent" in o)
step("user namespaces disabled", "unshare failed" in o or "Invalid argument" in o or "Operation not permitted" in o)
r = ex(S, "timeout 4 bash -c 'exec 3<>/dev/tcp/223.5.5.5/80' 2>&1; echo direct-rc=$?; timeout 4 bash -c 'exec 3<>/dev/tcp/${http_proxy#http://}' 2>&1 | head -1; echo proxy-reach=$?", "agent")
o = r.get("stdout", "")
step("direct egress dropped (only the proxy is reachable)", "direct-rc=0" not in o, o.strip().replace("\n", " | ")[:100])
r = ex(S, "echo hi > /home/agent/x && cat /home/agent/x"); step("writable home on the upper layer", r.get("stdout", "").strip() == "hi")
print("== kernel ABI (agent-runtime) ==")
c, r = req("POST", f"/sandboxes/{S}/exec", {"cmd": "ls -la /run/aios/abi.sock && test ! -r /run/aios/events.jsonl && echo log-hidden", "user": "agent", "timeout": 20})
o = r.get("stdout", ""); step("abi socket present, event log hidden from agent", "abi.sock" in o and "log-hidden" in o, o.strip().replace("\n", " | ")[:120])
c, ev = req("GET", f"/sandboxes/{S}/events?since=0"); step("host reads the kernel event log", c == 200 and any(e.get("type") == "kernel" for e in ev.get("events", [])), f"{len(ev.get('events', []))} events")
c, b = req("PUT", f"/sandboxes/{S}/budget", {"max_tokens": 1000}); step("host sets local budget limits", c == 200 and b.get("budget", {}).get("max_tokens") == 1000)
print("== landlock confinement of an exec ==")
c, r = req("POST", f"/sandboxes/{S}/exec", {"cmd": "touch /home/agent/ok && echo home-ok; touch /usr/bin/x 2>&1 | head -1; echo t > /tmp/t && echo tmp-ok; cat /etc/hostname >/dev/null && echo read-ok", "user": "agent", "timeout": 20, "landlock": True})
o = r.get("stdout", "") + r.get("stderr", "")
step("landlock: writes allowed in home and tmp, reads everywhere", "home-ok" in o and "tmp-ok" in o and "read-ok" in o, o.strip().replace("\n", " | ")[:140])
ex(S, "mkdir -p /home/agent/w")       # the policy below only allows writes inside w, so create it unconfined first
c, r = req("POST", f"/sandboxes/{S}/exec", {"cmd": "cd /home/agent/w && touch a && echo cwd-ok; touch /home/agent/outside 2>&1 | head -1; echo rc=$?", "user": "agent", "timeout": 20, "landlock": {"ro": ["/"], "rw": ["/home/agent/w", "/tmp"]}})
o = r.get("stdout", "") + r.get("stderr", "")
step("landlock: explicit policy denies writes outside the listed dirs", "cwd-ok" in o and "Permission denied" in o, o.strip().replace("\n", " | ")[:140])
r = ex(S, "touch /usr/bin/evil 2>&1; echo rc=$?"); step("base layer not writable by agent", "rc=0" not in r.get("stdout", ""), r.get("stdout", "").strip()[:60])
c, p = req("POST", f"/sandboxes/{S}/pack", {"name": f"tpl-test-{TPL}"}); step("pack writable layer into a new layer", c == 201 and p.get("bytes", 0) > 0, f"{p.get('bytes', 0) // 1048576} MiB in {p.get('s')}s")
req("DELETE", f"/sandboxes/{S}")
c, t = req("POST", "/templates/snapshot", {"template": TPL, "mem": 512, "vcpu": 1}); step("template snapshot", c in (200, 201), json.dumps(t)[:120])
if c in (200, 201):
    t0 = time.monotonic(); c, sb2 = req("POST", "/sandboxes", {"template": TPL, "class": "ephemeral", "mem": 512, "vcpu": 1, "boot": "snapshot", "ttl_idle_s": 300}); dt2 = time.monotonic() - t0
    ok = c == 201 and ex(sb2["id"], "true").get("exit") == 0
    step("boot from template snapshot", ok, f"{dt2:.2f}s boot_mode={sb2.get('boot_mode')}")
    if c == 201: req("DELETE", f"/sandboxes/{sb2['id']}")
    req("DELETE", f"/templates/{t.get('key')}")
if CMP:
    print(f"== compare with {CMP} ==")
    sbc, dtc, rc, fc = boot(CMP)
    step(f"{CMP} cold boot for comparison", sbc is not None and rc.get("exit") == 0, f"{dtc:.2f}s; fetched {fc and fc // 1048576} MiB")
    if sbc: req("DELETE", f"/sandboxes/{sbc['id']}")
    print(f"   boot: {TPL} {dt:.2f}s vs {CMP} {dtc:.2f}s; fetched: {fetched and fetched // 1048576} vs {fc and fc // 1048576} MiB")
