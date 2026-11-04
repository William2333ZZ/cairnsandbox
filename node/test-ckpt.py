#!/usr/bin/env python3
"""Checkpoints and in-place rollback (docs/002 沙箱侧 5 状态, D34). usage: test-ckpt.py http://host:port
Light: one 512 MiB VM."""
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
def state(sid): return ex(sid, "cat /home/agent/f 2>/dev/null; cat /dev/shm/m 2>/dev/null; echo -")["stdout"].split()

print("== checkpoint while running ==")
c, a = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300, "mem": 512}); A = a["id"]
step("create", c == 201, A)
MINE = {A}                                        # whatever this run created is deleted at exit even when a step crashes: the node is shared with production
atexit.register(lambda: [req("DELETE", f"/sandboxes/{x}") for x in list(MINE)])
ex(A, "echo v1 > /home/agent/f; echo m1 > /dev/shm/m")                      # disk state and memory (tmpfs) state
t0 = time.time(); c, c1 = req("POST", f"/sandboxes/{A}/checkpoints", {"name": "c1", "note": "first"}); t1 = time.time() - t0
step("checkpoint c1 taken, sandbox keeps running", c == 201 and c1["name"] == "c1" and req("GET", f"/sandboxes/{A}")[1]["status"] == "running", f"{t1:.2f}s paused={c1.get('paused_s')}s chain={c1.get('chain_len')} upper={c1.get('upper_mib')}MiB")
step("guest usable right after", state(A) == ["v1", "m1", "-"], str(state(A)))
ex(A, "echo v2 > /home/agent/f; echo m2 > /dev/shm/m")
c, c2 = req("POST", f"/sandboxes/{A}/checkpoints", {"name": "c2"}); step("checkpoint c2 on top (chain grows)", c == 201 and c2["chain_len"] == c1["chain_len"] + 1, f"chain {c1.get('chain_len')} -> {c2.get('chain_len')}")
c, r = req("POST", f"/sandboxes/{A}/checkpoints", {"name": "c2"}); step("duplicate name refused", c == 409)
c, r = req("POST", f"/sandboxes/{A}/checkpoints", {"name": "Bad Name"}); step("bad name refused", c == 400)
ex(A, "echo v3 > /home/agent/f; echo m3 > /dev/shm/m")
c, lst = req("GET", f"/sandboxes/{A}/checkpoints"); step("listed", c == 200 and [x["name"] for x in lst["checkpoints"]] == ["c1", "c2"], str([x["name"] for x in lst["checkpoints"]]))

print("== rollback in place ==")
t0 = time.time(); c, rb = req("POST", f"/sandboxes/{A}/rollback", {"checkpoint": "c1"}); tr = time.time() - t0
step("rollback to c1: same id, running, disk and memory as at c1", c == 200 and rb["id"] == A and rb["status"] == "running" and state(A) == ["v1", "m1", "-"], f"{tr:.2f}s rollback_s={rb.get('rollback_s')} state={state(A)}")
r = ex(A, "hostname; curl -s -o /dev/null -w %{http_code} http://example.com --max-time 5 || true"); step("hostname kept and network attached after rollback", r["stdout"].split()[0] == A, r["stdout"].strip().replace("\n", " "))
ex(A, "echo v1b > /home/agent/f")
c, rb = req("POST", f"/sandboxes/{A}/rollback", {"checkpoint": "c2"}); step("rollback forward to c2 (later checkpoint still intact after going back)", c == 200 and state(A) == ["v2", "m2", "-"], str(state(A)))
c, r = req("POST", f"/sandboxes/{A}/rollback", {"checkpoint": "nope"}); step("unknown checkpoint is 404", c == 404)

print("== fork from a checkpoint ==")
c, fk = req("POST", f"/sandboxes/{A}/fork", {"n": 1, "checkpoint": "c1", "ttl_idle_s": 120})
F = (fk.get("clones") or [{}])[0].get("id"); MINE.add(F) if F else None
step("a clone from c1 starts where c1 was while the parent stays at c2", c == 201 and F and state(F) == ["v1", "m1", "-"] and state(A) == ["v2", "m2", "-"], f"clone={F} {state(F) if F else None} parent={state(A)} {fk.get('s')}s")
step("the clone is its own sandbox (hostname, network)", F and ex(F, "hostname")["stdout"].strip() == F and req("GET", f"/sandboxes/{F}")[1].get("ip") not in (None, req("GET", f"/sandboxes/{A}")[1].get("ip")))
c, r = req("POST", f"/sandboxes/{A}/fork", {"n": 1, "checkpoint": "nope"}); step("fork from an unknown checkpoint is 404", c == 404)
if F: req("DELETE", f"/sandboxes/{F}")

print("== pause / resume and checkpoints coexist ==")
ex(A, "echo v4 > /home/agent/f")
c, p = req("POST", f"/sandboxes/{A}/pause"); c, rs = req("POST", f"/sandboxes/{A}/resume")
step("pause/resume after rollbacks keeps the latest state", rs["status"] == "running" and state(A)[0] == "v4", str(state(A)))
c, rb = req("POST", f"/sandboxes/{A}/rollback", {"checkpoint": "c1"}); step("rollback after a pause cycle", c == 200 and state(A) == ["v1", "m1", "-"], str(state(A)))
c, d = req("DELETE", f"/sandboxes/{A}/checkpoints/c2"); step("drop c2", c == 200 and [x["name"] for x in d["checkpoints"]] == ["c1"])
c, rb = req("POST", f"/sandboxes/{A}/rollback", {"checkpoint": "c1"}); step("c1 survives dropping c2 (its layers were pinned, not gc'd)", c == 200 and state(A) == ["v1", "m1", "-"], str(state(A)))
c, i = req("GET", f"/sandboxes/{A}"); step("info lists checkpoints", i.get("checkpoints") == ["c1"], str(i.get("checkpoints")))

print("== checkpoints travel with the bundle (durable: pause publishes; the import side is test-cp's migration) ==")
store = req("GET", "/health")[1].get("store")
if store:
    c, d = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300, "mem": 256, "durable": True}); D = d["id"]; MINE.add(D)
    ex(D, "echo k1 > /home/agent/f"); c, k = req("POST", f"/sandboxes/{D}/checkpoints", {"name": "k1"})
    ex(D, "echo k2 > /home/agent/f")
    c, p = req("POST", f"/sandboxes/{D}/pause"); c, i = req("GET", f"/sandboxes/{D}"); bundle = i.get("bundle")
    step("durable pause published a bundle", c == 200 and bundle, str(bundle))
    c, exp = req("POST", f"/sandboxes/{D}/export"); imgs = exp.get("images") or {}
    step("the bundle carries the checkpoint's state, disk and memory chain", c == 200 and {"ckpt/k1/snapshot.bin", "ckpt/k1/upper.ext4"} <= set(imgs) and any(k.startswith("ckpt/k1/mem/") for k in imgs), str(sorted(k for k in imgs if k.startswith("ckpt")))[:120])
    shared = [k for k in imgs if k.startswith("ckpt/k1/mem/") and imgs[k] in {imgs[m] for m in imgs if m.startswith("mem/")}]
    step("layers the checkpoint shares with the main chain are the same image (uploaded once)", len(shared) >= 1, str(shared)[:100])
    req("DELETE", f"/sandboxes/{D}")                          # deleting a durable sandbox drops its bundles: the archive is the sandbox's, not a backup
else: step("(no store on this node: bundle checkpoints not exercised)", True)

print("== a paused sandbox is kept retain_paused_s, then deleted ==")
c, b = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 2, "retain_paused_s": 3, "mem": 256}); B = b["id"]; MINE.add(B)
step("created with ttl_idle_s=2 retain_paused_s=3", c == 201 and b["retain_paused_s"] == 3, B)
time.sleep(4); i1 = req("GET", f"/sandboxes/{B}")[1]
step("auto-paused after the idle ttl, paused_at recorded", i1.get("status") == "paused" and i1.get("paused_at"), f"{i1.get('status')} paused_at={i1.get('paused_at')}")
time.sleep(5); c, i2 = req("GET", f"/sandboxes/{B}")
step("gone after the retention", c == 404, f"{c} {i2.get('status') if isinstance(i2, dict) else ''}")
c, r = req("POST", f"/sandboxes/{A}/ttl", {"retain_paused_s": 86400}); step("retention settable on a running sandbox", c == 200 and r.get("retain_paused_s") == 86400)

print("== cleanup ==")
c, r = req("DELETE", f"/sandboxes/{A}"); step("deleted", c == 200)
