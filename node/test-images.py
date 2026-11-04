#!/usr/bin/env python3
"""Images: a running sandbox committed as a named template (with its memory), and a layer pulled from an OCI image (docs/002 沙箱侧 4, D37).
usage: test-images.py http://node [oci-image | path-to-docker-save-tar]   (default busybox:latest from the registry; a path uses /layers/from-archive)
Light: at most two 512 MiB VMs alive at once."""
import json, os, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
IMAGE = sys.argv[2] if len(sys.argv) > 2 else "busybox:latest"
AUTH = {"Authorization": "Bearer " + os.environ["NODE_TOKEN"]} if os.environ.get("NODE_TOKEN") else {}

def req(method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]
SMALL = {"class": "ephemeral", "ttl_idle_s": 300, "mem": 512, "vcpu": 2}
TPL = f"warm-{int(time.time()) % 100000}"

print("== commit a running sandbox as a template ==")
c, a = req("POST", "/sandboxes", SMALL); A = a["id"]
ex(A, "echo prepared > /home/agent/f; echo inmem > /dev/shm/m; sleep 1000 >/dev/null 2>&1 & echo $! > /dev/shm/pid")
t0 = time.time(); c, t = req("POST", f"/sandboxes/{A}/commit", {"name": TPL, "note": "with a sleeper"}); tc = time.time() - t0
step("committed with memory, sandbox keeps running", c == 201 and t.get("key") == TPL and t.get("from") == A and req("GET", f"/sandboxes/{A}")[1]["status"] == "running", f"{tc:.2f}s mem={t.get('mem_mib')}MiB upper={t.get('upper_mib')}MiB err={t.get('error')}")
c, r = req("POST", f"/sandboxes/{A}/commit", {"name": TPL}); step("duplicate template name refused", c == 409)
c, r = req("POST", f"/sandboxes/{A}/commit", {"name": "9bad"}); step("bad template name refused", c == 400)
ex(A, "echo changed-after > /home/agent/f")
c, lst = req("GET", "/templates"); step("template listed with its origin", any(s.get("key") == TPL and s.get("from") == A for s in lst["snapshots"]))
t0 = time.time(); c, b = req("POST", "/sandboxes", {**SMALL, "template_key": TPL, "boot": "snapshot"}); tb = time.time() - t0; B = b.get("id")
step("new sandbox from the named template boots warm", c == 201 and b.get("boot_mode") == "snapshot" and b.get("template_snapshot") == TPL, f"{B} {tb:.2f}s err={b.get('error')}")
r = ex(B, "cat /home/agent/f; cat /dev/shm/m; kill -0 $(cat /dev/shm/pid) && echo sleeper-alive; hostname")
step("it has the committed disk, memory and processes, and its own hostname", r.get("stdout", "").split() == ["prepared", "inmem", "sleeper-alive", B], r.get("stdout", "").strip().replace("\n", " "))
c, r = req("POST", "/sandboxes", {**SMALL, "mem": 256, "template_key": TPL, "boot": "snapshot"}); step("a different guest configuration cannot use it", c == 400 and "was taken with" in str(r.get("error")), str(r.get("error"))[:70])
c, r = req("POST", "/sandboxes", {"template_key": TPL, "boot": "snapshot"}); step("a named template alone is enough: the guest config comes from it", c == 201, str(r.get("error"))[:70]); req("DELETE", f"/sandboxes/{r.get('id')}")
c, r = req("POST", "/sandboxes", {**SMALL, "template_key": "no-such-template", "boot": "snapshot"}); step("unknown named template refused", c == 400)
c, r = req("POST", "/sandboxes", {**SMALL, "template_key": "0123456789abcdef"}); step("a 16-hex key is a placement hint, not an error", c == 201); req("DELETE", f"/sandboxes/{r.get('id')}")
for s in (A, B): req("DELETE", f"/sandboxes/{s}")
c, r = req("DELETE", f"/templates/{TPL}"); step("template deleted", c == 200, str(r)[:60])

print("== a layer from an OCI image ==")
if os.path.isfile(IMAGE):                                                      # an archive from `docker save` (where the node cannot reach a registry)
    data = open(IMAGE, "rb").read()
    rq = urllib.request.Request(BASE + "/layers/from-archive?name=oci-test", data=data, method="POST", headers={"Content-Type": "application/x-tar", **AUTH})
    t0 = time.time()
    try:
        with urllib.request.urlopen(rq, timeout=1200) as resp: c, l = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: c, l = e.code, json.loads(e.read() or b"{}")
    tl = time.time() - t0
    step("archive unpacked into a layer", c == 201 and l.get("id", "").startswith("L-") and l["oci"]["layers"] >= 1, f"{l.get('id')} {tl:.1f}s archive={len(data)} bytes layer={l.get('bytes')} bytes err={l.get('error')}")
else:
    t0 = time.time(); c, l = req("POST", "/layers/from-oci", {"image": IMAGE, "name": "oci-test"}, timeout=1200); tl = time.time() - t0
    step("pulled and turned into a layer", c == 201 and l.get("id", "").startswith("L-") and l["oci"]["layers"] >= 1, f"{l.get('id')} {tl:.1f}s fetched={l.get('oci', {}).get('fetched_bytes')} bytes layer={l.get('bytes')} bytes err={l.get('error')}")
if c == 201:
    c, s = req("POST", "/sandboxes", {**SMALL, "layers": [l["id"]], "boot": "cold"}); S = s.get("id")
    r = ex(S, "ls -la /bin/busybox 2>/dev/null | head -1; /bin/busybox echo from-busybox 2>/dev/null; cat /etc/os-release | head -1")
    step("the image's files are in a sandbox stacked on our base, the base still works", "from-busybox" in r.get("stdout", "") and "PRETTY_NAME" in r.get("stdout", ""), r.get("stdout", "").strip().replace("\n", " | ")[:120])
    req("DELETE", f"/sandboxes/{S}")
c, r = req("POST", "/layers/from-oci", {"image": "definitely-not-an-image-zzz:latest", "name": "nope"}, timeout=600); step("a missing image fails with a registry error (or the registry is unreachable)", c == 502, str(r.get("error"))[:80])
rq = urllib.request.Request(BASE + "/layers/from-archive?name=bad", data=b"not a tar", method="POST", headers={**AUTH}); 
try:
    urllib.request.urlopen(rq, timeout=60); c = 200
except urllib.error.HTTPError as e: c = e.code
step("a bad archive is refused", c in (400, 500), str(c))
