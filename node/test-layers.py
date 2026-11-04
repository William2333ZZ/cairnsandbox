#!/usr/bin/env python3
"""Layers / pack_diff / memory-density / QoS tests. usage: test-layers.py http://host:port"""
import json, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
AUTH = {"Authorization": "Bearer " + __import__("os").environ["NODE_TOKEN"]} if __import__("os").environ.get("NODE_TOKEN") else {}
def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=600) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes
def ex(sid, cmd, **kw):
    c, r = req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})
    if "stdout" not in r: print("   exec error:", c, r); return {"stdout": "", "exit": None}
    return r
def mem(): return req("GET", "/health")[1]["node_mem_mib"]

BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}   # the node is shared: only sandboxes that appear during this run count as left behind
print("== pack_diff: build a layer from a sandbox ==")
c, a = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300}); A = a["id"]
r = ex(A, "mkdir -p /opt/tool && echo v1 > /opt/tool/VERSION && dd if=/dev/urandom of=/opt/tool/blob bs=1M count=20 2>/dev/null && rm -rf /var/log/apt && echo note > /home/agent/note; ls /var/log | head -3", user="root")
c, L = req("POST", f"/sandboxes/{A}/pack", {"name": "tool-v1"})
step("pack", c == 201, f"{L.get('id')} tar={L.get('tar_bytes',0)/1048576:.1f}MiB img={L.get('bytes',0)/1048576:.1f}MiB in {L.get('s')}s (tar {L.get('s_tar')}s, xfer {L.get('s_xfer')}s) warn={L.get('tar_warnings','')[:80]!r}")
lid = L["id"]

print("== new sandbox on top of the layer ==")
c, b = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300, "layers": [lid]}); B = b["id"]
step("create with layer", c == 201 and b.get("layers") == [lid], str(b.get("layers")))
r = ex(B, "cat /opt/tool/VERSION; md5sum /opt/tool/blob | cut -c1-8; cat /home/agent/note; findmnt -n -o OPTIONS / | tr ',' '\\n' | grep lowerdir")
step("layer content visible, lowerdir has the extra layer", r["stdout"].startswith("v1") and "note" in r["stdout"] and "layer1" in r["stdout"], r["stdout"].strip().replace("\n", " | "))
r = ex(B, "test -e /var/log/apt && echo PRESENT || echo WHITEOUT-OK")
step("whiteout from the packed layer honored", "WHITEOUT-OK" in r["stdout"], r["stdout"].strip())
ex(B, "echo v2 > /opt/tool/VERSION", user="root")
c, cc = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 120, "layers": [lid]}); C = cc["id"]
step("layer is immutable (C still sees v1 after B wrote v2)", ex(C, "cat /opt/tool/VERSION")["stdout"].strip() == "v1")
for sid in (A, B, C): req("DELETE", f"/sandboxes/{sid}")

N_VMS = 3   # keep the shared box quiet: 3 VMs are enough to show the sharing effect
print(f"== memory density: {N_VMS} VMs each reading the base layer, virtio-blk vs pmem+DAX ==")
READ = "find /usr/lib /usr/bin -type f -size -8M -exec cat {} + > /dev/null 2>&1; echo done"
res = {}
for mode in (False, True):
    m0 = mem(); ids = []
    for i in range(N_VMS):
        c, s = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 300, "pmem_base": mode, "mem": 512}); ids.append(s["id"])
        if c != 201: print("   create failed:", s); break
    r = ex(ids[0], "findmnt -n -o SOURCE,OPTIONS / ; findmnt -n -o SOURCE,OPTIONS /.oldroot 2>/dev/null; lsblk -o NAME,LABEL | head -5; mount | grep -E 'lower|pmem' | head -3", user="root")
    print(f"   mode pmem={mode}: {r['stdout'].strip().splitlines()[:2]}")
    t0 = time.time()
    for sid in ids: ex(sid, READ, timeout=300)
    time.sleep(2); m1 = mem()
    res[mode] = (m1 - m0, round(time.time() - t0, 1))
    print(f"   pmem={mode}: node cgroup memory delta {m1-m0} MiB for {N_VMS} VMs after reading base files ({res[mode][1]}s)")
    for sid in ids: req("DELETE", f"/sandboxes/{sid}")
    time.sleep(1)
step("pmem+DAX uses less host memory than virtio-blk", res[True][0] < res[False][0], f"blk={res[False][0]} MiB  pmem={res[True][0]} MiB")

print("== balloon free-page reporting ==")
for bal in (False, True):
    c, s = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 300, "balloon": bal, "mem": 512}); sid = s["id"]
    m0 = mem(); ex(sid, "python3 -c 'x=bytearray(300<<20); print(len(x))'", timeout=120); time.sleep(0.5); m1 = mem(); time.sleep(6); m2 = mem()
    print(f"   balloon={bal}: node mem +{m1-m0} MiB right after a 300 MiB alloc/free, {m2-m0:+d} MiB 6 s later")
    req("DELETE", f"/sandboxes/{sid}")

print("== QoS: latency-sensitive gets a core-scheduling cookie ==")
c, s = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 60, "qos": "ls"}); sid = s["id"]
c, au = req("GET", "/audit?n=5"); cs = [e.get("core_sched") for e in au["events"] if e["op"] == "create" and e["sandbox"] == sid]
step("core_sched cookie created", cs == ["ok"], str(cs)); req("DELETE", f"/sandboxes/{sid}")
c, sb_ = req("GET", "/sandboxes"); left_ = [x["id"] for x in sb_.get("sandboxes", []) if x["id"] not in BEFORE]; step("all gone", c == 200 and not left_, f"{len(left_)} left {left_[:3]}")
