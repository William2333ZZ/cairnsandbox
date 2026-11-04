#!/usr/bin/env python3
"""Control plane over several nodes vs one node: placement/admission, forwarding, templates everywhere, migration,
node loss + takeover, reconcile. usage: test-cp.py <cp-url> <nodeA> <nodeB> [stop-nodeA-cmd] [start-nodeA-cmd]
Light: at most four 256 MiB VMs alive at once. Optional env NODE_URL/NODE_TOKEN: direct-vs-forwarded latency."""
import json, os, subprocess, sys, time, urllib.request

CP, A, B = sys.argv[1], sys.argv[2], sys.argv[3]
STOP_A = sys.argv[4] if len(sys.argv) > 4 else None; START_A = sys.argv[5] if len(sys.argv) > 5 else None

def req(method, path, body=None, base=None, headers=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request((base or CP) + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:200].decode(errors="replace")})

import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]
def wait_nodes(alive, timeout=60):
    for _ in range(timeout * 2):
        try:
            c, h = req("GET", "/health", timeout=5)
            if all(h["nodes"].get(n, {}).get("alive") is a for n, a in alive.items()): return h
        except Exception: pass
        time.sleep(0.5)
    return req("GET", "/health")[1]
SMALL = {"mem": 256, "vcpu": 1, "ttl_idle_s": 300}

print("== cluster ==")
h = wait_nodes({A: True, B: True}); step("both nodes alive", h["nodes"][A]["alive"] and h["nodes"][B]["alive"], json.dumps(h["nodes"]))

print("== admission: one node vs the cluster ==")
pinned = []
while True:
    c, r = req("POST", "/sandboxes", {**SMALL, "class": "ephemeral", "node": A})
    if c != 201: break
    pinned.append(r["id"])
single_cap = len(pinned); step("single node fills up and refuses", c == 429 and single_cap >= 1, f"{A} admits {single_cap} VM(s), then {c}")
for sid in pinned: req("DELETE", f"/sandboxes/{sid}")
time.sleep(1)
spread = []
while True:
    c, r = req("POST", "/sandboxes", {**SMALL, "class": "ephemeral"})
    if c != 201: break
    spread.append(r)
by_node = {}
for r in spread: by_node[r["node"]] = by_node.get(r["node"], 0) + 1
step("cluster admits more than one node and spreads", c == 429 and len(spread) > single_cap and len(by_node) == 2, f"placed {by_node} (single node: {single_cap})")
print(f"   capacity: single node {single_cap}, two nodes {len(spread)}")
for r in spread: req("DELETE", f"/sandboxes/{r['id']}")
time.sleep(2.5)

print("== forwarding ==")
c, x = req("POST", "/sandboxes", {**SMALL, "class": "persistent"}); X = x["id"]
step("create via control plane", c == 201 and x["node"] in (A, B) and x.get("durable"), f"{X} on {x['node']} durable={x.get('durable')}")
r = ex(X, "echo hello > /home/agent/f; cat /home/agent/f"); step("exec forwarded", r.get("stdout", "").strip() == "hello", r.get("stdout", r))
rq = urllib.request.Request(CP + f"/sandboxes/{X}/exec/stream", data=json.dumps({"cmd": "for i in 1 2 3; do echo t$i; sleep 0.3; done"}).encode(), method="POST", headers={"Content-Type": "application/json"})
t0 = time.time(); got = []
with urllib.request.urlopen(rq, timeout=60) as resp:
    for line in resp: got.append((round(time.time() - t0, 2), json.loads(line)))
step("streaming exec forwarded chunk by chunk", len(got) >= 4 and got[-1][1].get("done") and got[2][0] - got[0][0] >= 0.4, str([(t, e.get("data", e.get("exit"))) for t, e in got]))
ex(X, "timeout 6 sh -c 'while :; do :; done' || true")             # D57: some CPU for the sampler to see
time.sleep(6)
c, mt = req("GET", f"/sandboxes/{X}/metrics"); pts = mt.get("points", []) if c == 200 else []
step("live metrics: the node samples the sandbox's cgroup (cpu, memory)", c == 200 and len(pts) >= 2 and pts[-1]["mem_mib"] > 0 and any((p["cpu_pct"] or 0) > 20 for p in pts), f"{c} {len(pts)} points, last {pts[-1] if pts else None}")
c, inf = req("GET", f"/sandboxes/{X}"); step("info carries the guest's cumulative CPU time (D67: the active-CPU billing basis)", c == 200 and isinstance(inf.get("cpu_usec"), (int, float)) and inf["cpu_usec"] > 1_000_000, f"cpu_usec={inf.get('cpu_usec')}")
ts = []
for _ in range(10):
    t0 = time.time(); ex(X, "true"); ts.append(time.time() - t0)
cp_ms = sorted(ts)[len(ts) // 2] * 1000
if os.environ.get("NODE_URL") and x["node"] == os.environ.get("NODE_NAME_OF_URL", x["node"]):
    hdr = {"Authorization": "Bearer " + os.environ["NODE_TOKEN"]}; td = []
    for _ in range(10):
        t0 = time.time(); req("POST", f"/sandboxes/{X}/exec", {"cmd": "true"}, base=os.environ["NODE_URL"], headers=hdr); td.append(time.time() - t0)
    print(f"   exec median: via control plane {cp_ms:.1f} ms, direct to node {sorted(td)[5]*1000:.1f} ms (hop overhead {cp_ms - sorted(td)[5]*1000:.1f} ms)")
else: print(f"   exec median via control plane {cp_ms:.1f} ms")

print("== template snapshot on every node ==")
c, te = req("POST", "/templates/everywhere", {"mem": 256, "vcpu": 1})
step("built once, imported on the other node", c == 200 and set(te["nodes"]) == {A, B} and all(v in ("built", "imported", "present") for v in te["nodes"].values()), f"{te.get('nodes')} in {te.get('s')}s mem={te.get('mem_mib')}MiB")
time.sleep(2.5)
c, y = req("POST", "/sandboxes", {**SMALL, "class": "persistent", "boot": "snapshot", "template_key": te["key"]}); Y = y["id"]
step("boot from the template snapshot through the control plane", c == 201 and y["boot_mode"] == "snapshot", f"{Y} on {y['node']}")

print("== migration ==")
src = y["node"]; dst = B if src == A else A
ex(Y, "echo before > /home/agent/m"); c, ck = req("POST", f"/sandboxes/{Y}/checkpoints", {"name": "mk"}); step("checkpoint before the move", c == 201, str(ck.get("name")))
ex(Y, "echo moved-$(hostname) > /home/agent/m; python3 -c \"x=bytearray(32<<20)\"")
c, mg = req("POST", f"/sandboxes/{Y}/migrate", {"to": dst})
step("migrate paused sandbox to the other node", c == 200 and mg["to"] == dst, f"export {mg.get('export_s')}s sent {mg.get('sent_mib')}MiB, import {mg.get('import_s')}s fetched {mg.get('fetched_mib')}MiB, total {mg.get('total_s')}s")
r = ex(Y, "cat /home/agent/m; hostname"); c, yi = req("GET", f"/sandboxes/{Y}")
step("resumed on the target with its state", r.get("stdout", "").split()[:1] == [f"moved-{Y}"] and yi.get("node") == dst, f"{r.get('stdout', '').split()} node={yi.get('node')}")
c, lst = req("GET", "/sandboxes"); step("listed exactly once", [s["id"] for s in lst["sandboxes"]].count(Y) == 1)
step("the checkpoint travelled in the bundle", yi.get("checkpoints") == ["mk"], str(yi.get("checkpoints")))
c, rb = req("POST", f"/sandboxes/{Y}/rollback", {"checkpoint": "mk"}); r = ex(Y, "cat /home/agent/m")
step("and the moved sandbox rolls back to it", c == 200 and r.get("stdout", "").strip() == "before", f"{c} {r.get('stdout', '').strip()}")

if STOP_A and START_A:
    print("== node loss and takeover ==")
    victim, other = X, Y
    if req("GET", f"/sandboxes/{victim}")[1].get("node") != A: req("POST", f"/sandboxes/{victim}/migrate", {"to": A})
    if req("GET", f"/sandboxes/{other}")[1].get("node") != B: req("POST", f"/sandboxes/{other}/migrate", {"to": B})
    ex(victim, "echo survived > /home/agent/marker")
    c, p = req("POST", f"/sandboxes/{victim}/pause"); step("durable pause published a bundle", c == 200 and p.get("bundle"), str(p.get("bundle")))
    subprocess.run(STOP_A, shell=True, check=True)
    h = wait_nodes({A: False}); step("control plane notices the node is down", h["nodes"][A]["alive"] is False, str(h["nodes"][A]["last_err"]))
    c, lst = req("GET", "/sandboxes"); st = {s["id"]: s["status"] for s in lst["sandboxes"]}
    step("its sandbox is reported unreachable, the other still served", st.get(victim) == "unreachable" and ex(other, "echo ok")["stdout"].strip() == "ok", str(st))
    c, r = req("POST", f"/sandboxes/{victim}/exec", {"cmd": "true"}); step("ops on it fail fast with 503", c == 503, str(r.get("error")))
    c, ev = req("POST", f"/nodes/{A}/evacuate")
    step("evacuate re-homes the durable sandbox from the store", c == 200 and [m["sandbox"] for m in ev["moved"]] == [victim] and not ev["lost"], json.dumps(ev)[:200])
    r = ex(victim, "cat /home/agent/marker"); c, vi = req("GET", f"/sandboxes/{victim}")
    step("taken-over sandbox resumes with the state from its last pause", r.get("stdout", "").strip() == "survived" and vi.get("node") == B, f"node={vi.get('node')}")
    subprocess.run(START_A, shell=True, check=True); h = wait_nodes({A: True}); time.sleep(3)
    c, lst = req("GET", "/sandboxes"); ids = [s["id"] for s in lst["sandboxes"]]
    step("node back: its stale local copy reconciled away, sandbox listed once", h["nodes"][A]["alive"] and ids.count(victim) == 1, f"{ids}")
    c, au = req("GET", "/routes"); print("   routes:", {k: v["node"] for k, v in au["routes"].items()})

print("== cleanup ==")
for sid in (X, Y): req("DELETE", f"/sandboxes/{sid}")
for _ in range(30):                                                                 # the drop of routes and the listing follow the deletes asynchronously: give them time, not a fixed 3 seconds
    time.sleep(1); c, h = req("GET", "/health"); c, lst = req("GET", "/sandboxes")
    if h["routes"] == 0 and not lst["sandboxes"]: break
c, bl = req("GET", "/store/bundles"); left = [b for b in bl["bundles"] if b.startswith("sb-") and (X in b or Y in b)]
step("destroyed sandboxes' bundles removed from the store", not left, str(left))
c, g = req("POST", "/store/gc"); print("   store gc:", g)
step("all gone", h["routes"] == 0 and not lst["sandboxes"] and all((n["total"] or 0) == 0 for n in h["nodes"].values() if n["alive"]), json.dumps(h["nodes"]))
