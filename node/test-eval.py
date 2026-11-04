#!/usr/bin/env python3
"""Product layer end to end: control plane + model proxy (mock) + evaluator + workbench static files.
usage: test-eval.py <cp-url>   (CP must have /eval forwarding and WEB_DIR; the evaluator needs the dev layer via EVAL_LAYERS)
Light: a handful of 512 MiB VMs, sequential batches."""
import atexit, json, sys, time, urllib.request

CP = sys.argv[1].rstrip("/")
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))

def req(method, path, body=None, raw=False, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(CP + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read(); return resp.status, (b if raw else (json.loads(b) if b else {}))
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:200].decode(errors="replace")})

def wait_batch(bid, timeout=600):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        c, b = req("GET", f"/eval/batches/{bid}")
        if b.get("status") in ("done", "failed"): return b, round(time.monotonic() - t0, 1)
        time.sleep(3)
    return b, round(time.monotonic() - t0, 1)

BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}   # sandboxes that were already there (other users of this node)
print("== services ==")
c, h = req("GET", "/health"); step("control plane up", c == 200 and any(n["alive"] for n in h["nodes"].values()), json.dumps(h["nodes"])[:120])
c, b = req("GET", "/ui/", raw=True); step("workbench served at /ui/", c == 200 and b"<html" in b.lower(), f"{len(b)} bytes")
c, b = req("GET", "/ui/../etc/passwd", raw=True); step("static path traversal refused", c == 404)
c, e = req("GET", "/eval/health"); step("evaluator reachable through /eval", c == 200 and e.get("ok") and "basic" in e.get("tasksets", []), json.dumps(e))
c, ts = req("GET", "/eval/tasksets/basic"); step("taskset listed with 10 tasks", c == 200 and len(ts["tasks"]) == 10)

print("== version ==")
c, vs = req("GET", "/eval/versions")
vid = next((v["id"] for v in vs.get("versions", []) if v["label"] == "ci-seed"), None)
if not vid:
    c, v = req("POST", "/eval/versions", {"label": "ci-seed", "layers": [], "note": "CI: evaluator default layers"}); vid = v.get("id")
step("version registered", bool(vid), vid)

print("== import checks (3 smoke tasks) ==")
smoke = [t["id"] for t in ts["tasks"] if t.get("smoke")] or [t["id"] for t in ts["tasks"][:3]]
smoke = smoke[:3]
c, r = req("POST", "/eval/tasksets/basic/check", {"tasks": smoke}); b, s = wait_batch(r["batch_id"])
checks = (b.get("summary") or {}).get("checks", {})
step("reference passes, noop fails, repeats consistent", b["status"] == "done" and all(all(cc.get(k) for k in ("reference_pass", "noop_fail", "repeat_consistent", "within_time")) for cc in checks.values()) and len(checks) == len(smoke), f"{s}s {json.dumps({t: cc.get('scores') for t, cc in checks.items()})[:200]}")
step("no infra failures during checks", b["progress"]["infra_fail"] == 0, json.dumps(b["progress"]))

print("== baseline with the mock model (k=2) ==")
c, r = req("POST", "/eval/batches", {"kind": "baseline", "taskset": "basic", "version": vid, "k": 2, "tasks": smoke, "label": "ci mock"})
step("batch accepted", c in (200, 201, 202) and r.get("batch_id"), json.dumps(r)); b, s = wait_batch(r["batch_id"])
st = b.get("stats") or {}
step("baseline done, every run has a score", b["status"] == "done" and b["progress"]["done"] == 2 * len(smoke) and all(x["score"] is not None for rs in b["matrix"].values() for x in rs), f"{s}s mean={st.get('mean')} delta={st.get('delta')}")
one = next(iter(b["matrix"].values()))[0]
step("runs carry token usage from the proxy", one["tokens_in"] > 0 and one["tokens_out"] > 0 and one["infra_fail"] is False, f"tokens {one['tokens_in']}/{one['tokens_out']} cost {one['cost']}")
c, run = req("GET", f"/eval/runs/{one['run_id']}")
step("run has a trajectory, agent exit and verify output", c == 200 and run.get("traj") and run["traj"]["info"]["exit_status"] == "Submitted" and run["agent_exit"] == 0 and run["verify"]["exit"] == 0, f"msgs={len(run['traj']['messages'])} score={run['verify']['score']}")
evs = run.get("events") or []
step("kernel ABI events recorded for the run (harness -> agent-runtime)", any(e.get("type") == "model_call" for e in evs) and any(e.get("type") == "exit" for e in evs), f"{len(evs)} events: {sorted(set(e.get('type') for e in evs))}")
step("deterministic mock -> zero noise band", st.get("delta") == 0.0, f"delta={st.get('delta')}")

print("== compare against the baseline (cascade, same version) ==")
c, r = req("POST", "/eval/batches", {"kind": "compare", "taskset": "basic", "version": vid, "k": 1, "tasks": smoke, "base_batch": b["id"], "cascade": True, "label": "ci compare"})
cb, s = wait_batch(r["batch_id"]); cst = cb.get("stats") or {}
step("compare finished with a decision", cb["status"] == "done" and cst.get("accept") in (True, False) and cst.get("reason"), f"{s}s accept={cst.get('accept')} reason={cst.get('reason')} diff={cst.get('delta') if 'paired' not in cst else cst['paired']}")
step("same version is not accepted as an improvement", cst.get("accept") is False)

print("== cleanup ==")
time.sleep(2); c, sb = req("GET", "/sandboxes")
left = [x["id"] for x in sb["sandboxes"] if x["id"] not in BEFORE]                        # the node is shared: only what appeared during this run counts
step("no sandboxes left behind", c == 200 and not left, f"{len(left)} left {left[:3]}")
