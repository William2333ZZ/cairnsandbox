#!/usr/bin/env python3
"""The sandbox layer's numbers (D44): create / exec / pause / resume / checkpoint / rollback / fork latency, memory per sandbox, density ramp.
usage: bench.py http://node:port [--n 5] [--ramp 16] [--mem 128] [--md out.md]   (NODE_TOKEN in the environment)
Light by design: at most --ramp small VMs at once (8 by default; the shared dev box is other people's too, so no ramp to the limit there — a density figure
is for a dedicated machine), one burst, everything deleted at exit. Not a stress test."""
import argparse, json, os, statistics, sys, time, urllib.request

ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--n", type=int, default=5); ap.add_argument("--ramp", type=int, default=8); ap.add_argument("--mem", type=int, default=128); ap.add_argument("--md", default="")
ap.add_argument("--no-snapshot", action="store_true", help="skip building a template snapshot for the spec (warm boots)"); ap.add_argument("--no-pmem", action="store_true", help="skip the pmem/DAX base variant")
A = ap.parse_args(); BASE = A.base.rstrip("/")
AUTH = {"Authorization": "Bearer " + os.environ["NODE_TOKEN"]} if os.environ.get("NODE_TOKEN") else {}

def req(m, p, b=None, timeout=300):
    r = urllib.request.Request(BASE + p, data=json.dumps(b).encode() if b is not None else None, method=m, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp: return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

MINE = set()
import atexit
atexit.register(lambda: [req("DELETE", f"/sandboxes/{s}") for s in list(MINE)])
def timed(f):
    t0 = time.perf_counter(); r = f(); return time.perf_counter() - t0, r
def stats(xs): xs = sorted(xs); return {"n": len(xs), "p50": xs[len(xs) // 2], "p95": xs[min(len(xs) - 1, int(len(xs) * 0.95))], "min": xs[0], "max": xs[-1]}
def ex(s, cmd): return req("POST", f"/sandboxes/{s}/exec", {"cmd": cmd})[1]

h = req("GET", "/health")[1]
print(f"node {h.get('node')} jail={h.get('jail')} uffd={h.get('uffd')} max_vms={h.get('max_vms')} mem_budget={h.get('mem_budget_mib')} templates={h.get('templates')} snapshots={h.get('template_snapshots')}")
R = {}
spec = {"class": "persistent", "ttl_idle_s": 600, "mem": A.mem, "vcpu": 1}

# 1. create (cold) + first exec
cold, first_exec, ids = [], [], []
for i in range(A.n):
    t, (c, a) = timed(lambda: req("POST", "/sandboxes", {**spec, "boot": "cold"}))
    assert c == 201, a; ids.append(a["id"]); MINE.add(a["id"]); cold.append(t)
    t, r = timed(lambda: ex(a["id"], "true")); first_exec.append(t)
R["create_cold_s"] = stats(cold); R["first_exec_s"] = stats(first_exec)
# 2. warm: build (or reuse) the template snapshot for exactly this spec, then create from it; first exec after a warm boot too
def warm_round(extra, label):
    t, (c, tp) = timed(lambda: req("POST", "/templates/snapshot", {**spec, **extra}, timeout=600))
    if c not in (200, 201): R[label + "_error"] = f"{c} {tp.get('error')}"; return
    R[label + "_build_s"] = round(t, 3) if c == 201 else "reused"
    warm, wexec = [], []
    for i in range(A.n):
        t, (c, a) = timed(lambda: req("POST", "/sandboxes", {**spec, **extra, "boot": "snapshot"}))
        if c != 201: R[label + "_error"] = f"{c} {a.get('error')}"; break
        MINE.add(a["id"]); warm.append(t); wexec.append(timed(lambda: ex(a["id"], "true"))[0])
        if a.get("boot_mode") != "snapshot": R[label + "_note"] = f"boot_mode={a.get('boot_mode')}"
        req("DELETE", f"/sandboxes/{a['id']}"); MINE.discard(a["id"])
    if warm: R[label + "_s"] = stats(warm); R[label + "_first_exec_s"] = stats(wexec)
if not A.no_snapshot: warm_round({}, "create_snapshot")
# 2b. the pmem/DAX base (host page cache shared by every VM): cold and warm, and memory per sandbox while holding n of them
if not A.no_pmem:
    pm_ids, pm_cold = [], []
    for i in range(A.n):
        t, (c, a) = timed(lambda: req("POST", "/sandboxes", {**spec, "pmem_base": True, "boot": "cold"}))
        if c != 201: R["pmem_error"] = f"{c} {a.get('error')}"; break
        MINE.add(a["id"]); pm_ids.append(a["id"]); pm_cold.append(t); ex(a["id"], "true")
    if pm_cold: R["pmem_create_cold_s"] = stats(pm_cold)
    if pm_ids:
        time.sleep(2); before = req("GET", "/health")[1].get("node_mem_mib")
        for s in pm_ids: req("DELETE", f"/sandboxes/{s}"); MINE.discard(s)
        time.sleep(3); after = req("GET", "/health")[1].get("node_mem_mib")
        if before is not None and after is not None: R["pmem_node_mem_per_sandbox_mib"] = round((before - after) / len(pm_ids), 1)
        if not A.no_snapshot: warm_round({"pmem_base": True}, "pmem_create_snapshot")
# 3. exec round trip, pause, resume, checkpoint, rollback, fork on the first sandbox
S = ids[0]
R["exec_true_s"] = stats([timed(lambda: ex(S, "true"))[0] for _ in range(10)])
pause, resume, ck, rb, fk = [], [], [], [], []
for i in range(A.n):
    pause.append(timed(lambda: req("POST", f"/sandboxes/{S}/pause"))[0]); resume.append(timed(lambda: req("POST", f"/sandboxes/{S}/resume"))[0])
R["pause_s"], R["resume_s"] = stats(pause), stats(resume)
for i in range(A.n):
    t, (c, r) = timed(lambda: req("POST", f"/sandboxes/{S}/checkpoints", {"name": f"b{i}"})); ck.append(t)
    t, _ = timed(lambda: req("POST", f"/sandboxes/{S}/rollback", {"checkpoint": f"b{i}"})); rb.append(t)
R["checkpoint_s"], R["rollback_s"] = stats(ck), stats(rb)
for i in range(A.n):
    t, (c, f) = timed(lambda: req("POST", f"/sandboxes/{S}/fork", {"n": 1, "ttl_idle_s": 120})); fk.append(t)
    for cl in f.get("clones", []): req("DELETE", f"/sandboxes/{cl['id']}")
R["fork1_s"] = stats(fk)
# 4. memory per sandbox: node cgroup memory while holding the n sandboxes idle, vs after deleting them
time.sleep(2); held = req("GET", "/health")[1]
for s in ids: req("DELETE", f"/sandboxes/{s}"); MINE.discard(s)
time.sleep(3); empty = req("GET", "/health")[1]
if held.get("node_mem_mib") is not None and empty.get("node_mem_mib") is not None:
    R["node_mem_per_sandbox_mib"] = round((held["node_mem_mib"] - empty["node_mem_mib"]) / max(1, len(ids)), 1)
    R["node_mem_idle_mib"] = empty["node_mem_mib"]
# 5. density ramp: small sandboxes until --ramp or the node refuses; create latency under load, memory per sandbox at the end
ramp, ramp_ids, refused = [], [], None
for i in range(A.ramp):
    t, (c, a) = timed(lambda: req("POST", "/sandboxes", {**spec, "class": "ephemeral", "boot": "cold"}))
    if c != 201: refused = (c, a.get("error")); break
    ramp.append(t); ramp_ids.append(a["id"]); MINE.add(a["id"])
time.sleep(2); full = req("GET", "/health")[1]
R["ramp"] = {"reached": len(ramp_ids), "refused": refused, "create_s": stats(ramp) if ramp else None, "mem_mib_running": full.get("mem_mib_running"),
             "node_mem_mib": full.get("node_mem_mib"), "node_mem_per_sandbox_mib": round((full.get("node_mem_mib", 0) - (empty.get("node_mem_mib") or 0)) / max(1, len(ramp_ids)), 1) if full.get("node_mem_mib") is not None else None}
R["ramp"]["exec_under_load_s"] = stats([timed(lambda: ex(ramp_ids[-1], "true"))[0] for _ in range(5)]) if ramp_ids else None
for s in ramp_ids: req("DELETE", f"/sandboxes/{s}"); MINE.discard(s)

def row(k, v):
    if isinstance(v, dict) and "p50" in v: return f"| {k} | {v['p50']*1000:.0f} ms | {v['p95']*1000:.0f} ms | {v['min']*1000:.0f}–{v['max']*1000:.0f} ms (n={v['n']}) |"
    return f"| {k} | {v} | | |"
lines = [f"# 沙箱层数字 {time.strftime('%Y-%m-%d %H:%M')}", "", f"节点 `{h.get('node')}`,jail={h.get('jail')},uffd={h.get('uffd')},MAX_VMS={h.get('max_vms')},内存预算 {h.get('mem_budget_mib')} MiB;每台 {A.mem} MiB / 1 vCPU;{A.n} 次取 p50 / p95。", "",
         "| 指标 | p50 | p95 | 范围 |", "| --- | --- | --- | --- |"]
for k in ("create_cold_s", "create_snapshot_s", "create_snapshot_first_exec_s", "pmem_create_cold_s", "pmem_create_snapshot_s", "first_exec_s", "exec_true_s", "pause_s", "resume_s", "checkpoint_s", "rollback_s", "fork1_s"):
    if k in R: lines.append(row(k, R[k]))
for k in ("create_snapshot_build_s", "create_snapshot_error", "create_snapshot_note", "pmem_create_snapshot_build_s", "pmem_error", "pmem_create_snapshot_error", "pmem_node_mem_per_sandbox_mib"):
    if k in R: lines.append(row(k, R[k]))
lines += ["", f"- 节点容器内存:空载 {R.get('node_mem_idle_mib')} MiB;持 {len(ids)} 台空闲沙箱时每台约 **{R.get('node_mem_per_sandbox_mib')} MiB**(cgroup memory.current 的增量,含 VMM、页缓存)。",
          f"- 密度爬坡:连开 {R['ramp']['reached']} 台(上限 {A.ramp};{'被拒:' + str(R['ramp']['refused']) if R['ramp']['refused'] else '未触顶'}),爬坡中创建 p50 {R['ramp']['create_s']['p50']*1000:.0f} ms / p95 {R['ramp']['create_s']['p95']*1000:.0f} ms;满载时每台约 {R['ramp']['node_mem_per_sandbox_mib']} MiB,exec 往返 p50 {R['ramp']['exec_under_load_s']['p50']*1000:.0f} ms。" if R["ramp"]["create_s"] else "- 密度爬坡:没有创建成功。",
          "", "```json", json.dumps(R, indent=1, ensure_ascii=False), "```"]
out = "\n".join(lines); print(out)
if A.md: open(A.md, "w", encoding="utf-8").write(out + "\n")
