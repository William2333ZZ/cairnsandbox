#!/usr/bin/env python3
"""Volumes: ext4 images with their own life, attached as block devices (docs/002 沙箱侧 5, D35). usage: test-vol.py http://host:port
Light: at most three 512 MiB VMs alive at once."""
import json, os, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
AUTH = {"Authorization": "Bearer " + os.environ["NODE_TOKEN"]} if os.environ.get("NODE_TOKEN") else {}

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
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]
SMALL = {"class": "ephemeral", "ttl_idle_s": 300, "mem": 512}

print("== create volume ==")
c, v = req("POST", "/volumes", {"name": "data", "size_mib": 128}); V = v.get("id")
step("volume created (ext4 image, sparse)", c == 201 and v["size_mib"] == 128 and v["allocated_mib"] < 32, f"{V} allocated={v.get('allocated_mib')}MiB")
c, r = req("POST", "/volumes", {"name": "Bad Name"}); step("bad name refused", c == 400)
c, r = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": "vol-nope"}]}); step("unknown volume refused", c == 400)

print("== writer ==")
c, a = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": V, "mount": "/mnt/data"}]}); A = a.get("id")
step("sandbox with the volume attached rw (cold boot)", c == 201 and a["boot_mode"] == "cold" and a["volumes"][0]["id"] == V, f"{A} {a.get('boot_mode')}")
r = ex(A, "findmnt -no SOURCE,OPTIONS /mnt/data; id -un; touch /mnt/data/w && echo hello > /mnt/data/f && cat /mnt/data/f")
step("mounted rw at /mnt/data, writable by agent", "hello" in r.get("stdout", "") and "/dev/vd" in r.get("stdout", ""), r.get("stdout", "").strip().replace("\n", " "))
c, vi = req("GET", f"/volumes/{V}"); step("attachment recorded", vi.get("attached", {}).get(A) == "rw", str(vi.get("attached")))
c, b = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": V, "ro": True}]}); step("a reader cannot join while a writer holds it", c == 429, str(b.get("error"))[:80])
c, r = req("DELETE", f"/volumes/{V}"); step("cannot delete an attached volume", c == 409, str(r.get("error")))
ex(A, "sync")
c, r = req("DELETE", f"/sandboxes/{A}"); step("writer gone, volume detached", c == 200 and req("GET", f"/volumes/{V}")[1].get("attached") == {})

print("== readers share ==")
c, b = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": V, "ro": True}]}); B = b.get("id")
c2, d = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": V, "ro": True, "mount": "/srv/d"}]}); D = d.get("id")
step("two readers attached at once", c == 201 and c2 == 201, f"{B} {D}")
rb = ex(B, "cat /mnt/data/f; touch /mnt/data/x 2>&1 | tail -c 60", user="root"); rd = ex(D, "cat /srv/d/f")
step("both see the writer's data; read-only is enforced even for root", rb.get("stdout", "").startswith("hello") and "ead-only" in rb.get("stdout", "") and rd.get("stdout", "").strip() == "hello", (rb.get("stdout", "") + "|" + rd.get("stdout", "")).strip().replace("\n", " "))
c, w = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": V}]}); step("a writer cannot join while readers hold it", c == 429, str(w.get("error"))[:80])
c, p = req("POST", f"/sandboxes/{B}/pause"); c, rs = req("POST", f"/sandboxes/{B}/resume")
step("pause/resume keeps the volume mounted", rs["status"] == "running" and ex(B, "cat /mnt/data/f").get("stdout", "").strip() == "hello")
c, lst = req("GET", "/volumes"); step("listed with attachments", any(x["id"] == V and set(x["attached"]) == {B, D} for x in lst["volumes"]))

print("== a volume moves as a sparse tar (export / import keep the id and the data) ==")
r = urllib.request.Request(BASE + f"/volumes/{V}/export", headers=AUTH)
try:
    with urllib.request.urlopen(r, timeout=300) as resp: tarb = resp.read(); ce = resp.status
except urllib.error.HTTPError as e: tarb = b""; ce = e.code
step("exported while only readers hold it", ce == 200 and len(tarb) > 1024, f"{ce} {len(tarb)} bytes")
r = urllib.request.Request(BASE + "/volumes/import", data=tarb, method="POST", headers={"Content-Type": "application/x-tar", **AUTH})
try:
    with urllib.request.urlopen(r, timeout=300) as resp: ci = resp.status
except urllib.error.HTTPError as e: ci = e.code
step("importing it where it already exists is 409", ci == 409, str(ci))
for s in (B, D): req("DELETE", f"/sandboxes/{s}")
c, r = req("DELETE", f"/volumes/{V}"); step("volume deleted once free", c == 200)
r = urllib.request.Request(BASE + "/volumes/import", data=tarb, method="POST", headers={"Content-Type": "application/x-tar", **AUTH})
try:
    with urllib.request.urlopen(r, timeout=300) as resp: ci = resp.status; imp = json.loads(resp.read())
except urllib.error.HTTPError as e: ci = e.code; imp = {}
step("imported back under the same id", ci == 201 and imp.get("id") == V and imp.get("name") == "data", f"{ci} {imp.get('id')}")
c, e2 = req("POST", "/sandboxes", {**SMALL, "volumes": [{"id": V, "ro": True}]}); E = e2.get("id")
step("the imported volume still holds the data", c == 201 and ex(E, "cat /mnt/data/f").get("stdout", "").strip() == "hello", str(ex(E, "cat /mnt/data/f").get("stdout", ""))[:40] if E else str(e2)[:80])
if E: req("DELETE", f"/sandboxes/{E}")
c, r = req("DELETE", f"/volumes/{V}"); step("and is deleted again", c == 200)
c, r = req("GET", f"/volumes/{V}"); step("gone", c == 404)
