#!/usr/bin/env python3
"""Jail checks: unprivileged uid, chroot, cgroup limits, OOM containment. Needs docker access to inspect the node
container. usage: test-jail.py http://host:port <container-name>"""
import json, subprocess, sys, time, urllib.request

BASE, CTR = sys.argv[1], sys.argv[2]
AUTH = {"Authorization": "Bearer " + __import__("os").environ["NODE_TOKEN"]} if __import__("os").environ.get("NODE_TOKEN") else {}
def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=300) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]
def inctr(cmd): return subprocess.run(["docker", "exec", CTR, "bash", "-c", cmd], capture_output=True, text=True).stdout.strip()

c, a = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300, "mem": 512, "network": {"allow_hosts": ["mirrors.aliyun.com"]}}); A = a["id"]
step("create in jail", c == 201 and a.get("jail"), str(a.get("jail")))
pid = a["jail"]["pid"]; uid = a["jail"]["uid"]
ps = inctr(f"ps -o uid=,comm= -p {pid}")
step("vmm runs as the jail uid, not root", ps.split()[:1] == [str(uid)], ps)
mi = inctr(f"awk '$5==\"/\" {{print $4}}' /proc/{pid}/mountinfo")   # root of the mount at "/" in the VMM's mount namespace
step("vmm is chrooted (its / is the jail dir, not the container root)", f"/jail/firecracker/{A}/root" in mi, mi[:200])
cg = inctr(f"cat /proc/{pid}/cgroup; cat /sys/fs/cgroup/fc/{A}/memory.max /sys/fs/cgroup/fc/{A}/cpu.max /sys/fs/cgroup/fc/{A}/pids.max 2>&1")
step("per-VM cgroup with memory/cpu/pids limits", f"/fc/{A}" in cg and "805306368" in cg and "200000 100000" in cg, cg.replace("\n", " | "))
caps = inctr(f"grep CapEff /proc/{pid}/status")
step("vmm has no capabilities", caps.endswith("0000000000000000"), caps)
r = ex(A, "if command -v curl >/dev/null; then curl -s -o /dev/null -m 10 -w '%{http_code}' https://mirrors.aliyun.com/pypi/simple/ || echo ERR; else python3 -c \"import urllib.request\ntry: print(urllib.request.urlopen('https://mirrors.aliyun.com/pypi/simple/',timeout=10).status)\nexcept Exception as e: print(getattr(e,'code','') or type(e).__name__)\"; fi")
step("network works from the jailed VM (tap owned by uid)", r["stdout"].strip() == "200", r["stdout"].strip())

BASE_LEFT = [int(x) for x in inctr("ls /var/lib/fc-node/jail/firecracker 2>/dev/null | wc -l; ls /sys/fs/cgroup/fc | grep -c sb- || true; mount | grep -c '/jail/' || true").split()]   # other sandboxes on this shared node: ours must not add to these counts
print("== snapshot/fork/pause inside the jail ==")
c, f = req("POST", f"/sandboxes/{A}/fork", {"n": 2, "ttl_idle_s": 120}); clones = [x["id"] for x in f.get("clones", [])]
step("fork in jail", c == 201 and len(clones) == 2, str(f)[:200])
uids = {sid: req("GET", f"/sandboxes/{sid}")[1]["jail"]["uid"] for sid in [A] + clones}
step("clones get distinct uids", len(set(uids.values())) == 3, str(uids))
req("POST", f"/sandboxes/{A}/pause"); r = ex(A, "echo resumed-$(id -u)")
step("pause + transparent resume in jail", r["stdout"].strip() == "resumed-2000", r["stdout"].strip())

print("== OOM is contained ==")
c, b = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 120, "mem": 256}); B = b["id"]
r = ex(B, "python3 -c \"x=bytearray(700<<20); print('allocated')\"", timeout=60)
st = req("GET", f"/sandboxes/{B}")[1]["status"]
step("guest over-allocation does not escape its cgroup (vmm killed/guest OOM, node fine)", r.get("exit") != 0 or st.startswith("failed") or "allocated" not in r.get("stdout", ""), f"exit={r.get('exit')} status={st}")
h = req("GET", "/health")[1]; step("node still healthy", h["ok"], str(h["vms_running"]))

print("== cleanup ==")
for sid in [A] + clones + [B]: req("DELETE", f"/sandboxes/{sid}")
left = inctr("ls /var/lib/fc-node/jail/firecracker 2>/dev/null | wc -l; ls /sys/fs/cgroup/fc | grep -c sb- || true; mount | grep -c '/jail/' || true")
step("jail dirs, cgroups and bind mounts cleaned (none added beyond what was there)", [int(x) for x in left.split()] <= BASE_LEFT, f"{left.replace(chr(10), ' ')} vs before {BASE_LEFT}")
