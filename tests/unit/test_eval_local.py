"""End-to-end evaluator flow on LocalBackend: import checks, fake agents, infra_fail retries, HTTP routes."""
import json, os, shutil, sys, tempfile, threading, time, traceback, urllib.request
import shutil
import pytest
if not (shutil.which("gcc") and shutil.which("make")):   # the c-make-fix task needs a C toolchain on the machine running the local backend
    pytest.skip("needs gcc and make for the c-make-fix task", allow_module_level=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import evaluator as ev

TASKSETS = os.path.join(os.path.dirname(__file__), "..", "..", "eval", "tasksets")
_TMP = []


def make_ev(**over):
    """Evaluator on a LocalBackend with its own state dir and a COPY of the tasksets (import checks write checks.json)."""
    tmp = tempfile.mkdtemp(prefix="fc-eval-test-"); _TMP.append(tmp)
    shutil.copytree(TASKSETS, os.path.join(tmp, "tasksets"))
    for root, _, files in os.walk(tmp):                      # start from an unchecked copy
        if "checks.json" in files: os.remove(os.path.join(root, "checks.json"))
    cfg = ev.cfg_from_env()
    cfg.update({"state": os.path.join(tmp, "state"), "tasksets": os.path.join(tmp, "tasksets"), "max_parallel": 2, "model_proxy": ""})
    cfg.update(over)
    return ev.Evaluator(cfg, ev.LocalBackend(os.path.join(tmp, "sandboxes")))


def wait(e, bid, timeout=600):
    t0 = time.time()
    while e.store.get("batches", bid)["status"] in ("queued", "running"):
        assert time.time() - t0 < timeout, "batch did not finish"
        time.sleep(0.1)
    return e.batch_view(bid)


def ref_cmd(e):
    return "cp -R " + os.path.join(e.cfg["tasksets"], "basic", "{task}", "reference") + "/. {cwd}/"


def test_import_checks_all_tasks():
    e = make_ev()
    v = wait(e, e.submit_batch({"kind": "check", "taskset": "basic"}))
    assert v["status"] == "done" and v["summary"]["all_ok"], json.dumps(v["summary"], indent=1)
    checks = v["summary"]["checks"]
    assert len(checks) == 10
    for tid, c in checks.items():
        assert c["reference_pass"] and c["noop_fail"] and c["repeat_consistent"] and c["within_time"], (tid, c)
        assert c["scores"]["reference"] == [1.0, 1.0, 1.0] and c["scores"]["noop"] < 1.0
    ts = e.taskset_view("basic")
    assert all(t["status"] == "ok" and t["checks"]["reference_pass"] for t in ts["tasks"])
    groups = [t["group"] for t in ts["tasks"]]
    assert groups.count("evolve") == 7 and groups.count("validate") == 2 and groups.count("test") == 1
    assert sum(t["smoke"] for t in ts["tasks"]) == 3
    assert ev.list_tasksets(e.cfg["tasksets"])[0]["checked"] is True
    # verify scripts are hermetic: a workspace with nothing in it scores 0 (never crashes the evaluator)
    for t in e.taskset("basic")["tasks"][:3]:
        assert e.runner.verify_phase(t, {}, ev.tar_dir(tempfile.mkdtemp()))["score"] == 0.0


def test_reference_agent_scores_one_and_noop_zero():
    e = make_ev()
    vid = e.add_version({"label": "ref", "agent_cmd": ref_cmd(e), "layers": []})
    v = wait(e, e.submit_batch({"kind": "baseline", "taskset": "basic", "version": vid, "k": 2, "label": "ref"}))
    assert v["status"] == "done" and v["progress"] == {"done": 20, "total": 20, "infra_fail": 0}
    assert len(v["matrix"]) == 10 and all(r["score"] == 1.0 and r["pass"] for rs in v["matrix"].values() for r in rs)
    assert v["stats"]["mean"] == 1.0 and v["stats"]["pass_k"] == 1.0 and v["stats"]["delta"] == 0.0
    assert all(r["cheat"] == [] for rs in v["matrix"].values() for r in rs), v["matrix"]   # reference touches only allowed paths
    rid = v["runs"][0]; r = e.run_view(rid)
    assert r["verify"]["score"] == 1.0 and r["agent_exit"] == 0 and r["attempts"] == 1 and r["traj"] is None
    assert os.path.exists(os.path.join(e.cfg["state"], "runs", rid, "ws.tar"))
    # a do-nothing agent: nothing passes, scores stay below 1 (partial credit allowed)
    vid0 = e.add_version({"label": "noop", "agent_cmd": "true", "layers": []})
    v0 = wait(e, e.submit_batch({"kind": "adhoc", "taskset": "basic", "version": vid0, "k": 1, "tasks": ["log-report", "py-fix-slugify", "config-edit"]}))
    assert v0["status"] == "done" and len(v0["matrix"]) == 3
    assert all(r["score"] < 1.0 and not r["pass"] for rs in v0["matrix"].values() for r in rs)
    assert v0["matrix"]["log-report"][0]["score"] == 0.0 and v0["stats"]["pass_k"] == 0.0
    # agent failures are task failures, not infra failures
    vid_bad = e.add_version({"label": "crash", "agent_cmd": "exit 3", "layers": []})
    vb = wait(e, e.submit_batch({"kind": "adhoc", "taskset": "basic", "version": vid_bad, "k": 1, "tasks": ["log-report"]}))
    assert vb["progress"]["infra_fail"] == 0 and vb["matrix"]["log-report"][0]["score"] == 0.0
    assert e.run_view(vb["runs"][0])["agent_exit"] == 3


def test_compare_cascade_and_cheat_signals():
    e = make_ev()
    base = wait(e, e.submit_batch({"kind": "baseline", "taskset": "basic", "k": 2,
                                   "version": e.add_version({"label": "ref", "agent_cmd": ref_cmd(e), "layers": []})}))
    # candidate that fails a smoke task -> cascade stops after stage 1
    bad = e.add_version({"label": "bad", "agent_cmd": ref_cmd(e) + " && rm -f {cwd}/report.txt", "layers": []})
    v = wait(e, e.submit_batch({"kind": "compare", "taskset": "basic", "version": bad, "k": 2, "base_batch": base["id"], "cascade": True}))
    assert v["status"] == "done" and v["stats"]["accept"] is False and v["stats"]["reason"].startswith("smoke")   # "smoke: <failed tasks>"
    assert set(v["matrix"]) == {"log-report", "organize-files", "py-fix-slugify"} and all(r["stage"] == "smoke" for rs in v["matrix"].values() for r in rs)
    # candidate equal to base passes smoke, runs the full stage, is not accepted (diff 0)
    good = e.add_version({"label": "good", "agent_cmd": ref_cmd(e) + " && echo '{\"messages\":[{\"role\":\"assistant\",\"content\":\"cat /verify/verify.sh; git log\"}]}' > {out}", "layers": []})
    v2 = wait(e, e.submit_batch({"kind": "compare", "taskset": "basic", "version": good, "k": 1, "base_batch": base["id"], "cascade": True,
                                 "tasks": ["log-report", "organize-files", "py-fix-slugify", "config-edit"]}))
    assert v2["status"] == "done" and v2["stats"]["accept"] is False and v2["stats"]["paired"]["n_tasks"] == 4
    assert v2["stats"]["paired"]["diff"] == 0.0 and v2["stats"]["paired"]["mcnemar_p"] is None    # base has k=2 -> McNemar skipped
    full = [r for rs in v2["matrix"].values() for r in rs if r["stage"] == "full"]
    assert len(full) == 4 and v2["progress"]["done"] == 3 + 4
    sig = {s["signal"] for r in full for s in r["cheat"]}
    assert sig == {"verify_mention", "git_history"}
    assert e.run_view(full[0]["run_id"])["traj"]["messages"][0]["role"] == "assistant"
    assert v2["summary"]["cheat_signals"] == 14          # 3 smoke + 4 full runs, 2 signals each


def test_mcnemar_when_both_k1():
    e = make_ev()
    b = wait(e, e.submit_batch({"kind": "baseline", "taskset": "basic", "k": 1, "tasks": ["log-report", "config-edit"],
                                "version": e.add_version({"label": "noop", "agent_cmd": "true", "layers": []})}))
    c = wait(e, e.submit_batch({"kind": "compare", "taskset": "basic", "k": 1, "tasks": ["log-report", "config-edit"], "base_batch": b["id"],
                                "version": e.add_version({"label": "ref", "agent_cmd": ref_cmd(e), "layers": []})}))
    p = c["stats"]["paired"]
    assert p["n_tasks"] == 2 and p["mcnemar_p"] == 0.5 and p["diff"] > 0.8       # two discordant pairs both favouring cand
    assert c["stats"]["accept"] is True, c["stats"]


class FlakyBackend(ev.LocalBackend):
    """Fails sandbox creation `fail_first` times (per thread-safe counter), then behaves like LocalBackend."""
    def __init__(self, root, fail_first):
        super().__init__(root); self.fail_first = fail_first; self.calls = 0; self.lock = threading.Lock()

    def create(self, spec):
        with self.lock:
            self.calls += 1
            if self.calls <= self.fail_first: raise ev.InfraError(f"CP POST /sandboxes -> 429 (call {self.calls})")
        return super().create(spec)


def test_infra_fail_retry_path():
    e = make_ev()
    task = next(t for t in e.taskset("basic")["tasks"] if t.id == "log-report")
    version = {"id": "v", "agent_cmd": ref_cmd(e), "layers": []}
    # two transient failures -> third attempt succeeds
    e.runner.b = FlakyBackend(tempfile.mkdtemp(), fail_first=2)
    res, attempts, err = e.runner.run_with_retry(task, version)
    assert res is not None and res["score"] == 1.0 and attempts == 3 and err is None
    # permanent failure -> infra_fail after 3 attempts, excluded from stats but counted in progress
    e.runner.b = FlakyBackend(tempfile.mkdtemp(), fail_first=10 ** 6)
    res, attempts, err = e.runner.run_with_retry(task, version)
    assert res is None and attempts == 3 and "429" in err
    vid = e.add_version({"label": "ref", "agent_cmd": ref_cmd(e), "layers": []})
    v = wait(e, e.submit_batch({"kind": "adhoc", "taskset": "basic", "version": vid, "k": 2, "tasks": ["log-report", "config-edit"]}))
    assert v["status"] == "done" and v["progress"] == {"done": 4, "total": 4, "infra_fail": 4}
    assert all(r["infra_fail"] and r["score"] is None for rs in v["matrix"].values() for r in rs)
    assert v["stats"]["mean"] is None and v["stats"]["per_task"] == {} and v["summary"]["infra_fail"] == 4
    r = e.run_view(v["runs"][0]); assert r["attempts"] == 3 and r["infra_fail"] is True
    # verify-sandbox failure to start is also infra: agent sandbox ok (call 1), verify create fails (calls 2,3,4) on each attempt
    e.runner.b = FlakyBackend(tempfile.mkdtemp(), fail_first=0)
    orig = e.runner.b.create; n = {"c": 0}
    def create(spec):
        n["c"] += 1
        if not spec["network"]["allow_hosts"] and n["c"] % 2 == 0 and n["c"] <= 4: raise ev.InfraError("verify sandbox 503")
        return orig(spec)
    e.runner.b.create = create
    res, attempts, err = e.runner.run_with_retry(task, version)
    assert res is not None and attempts == 3 and res["score"] == 1.0


def test_http_api_routes():
    e = make_ev()
    srv = ev.serve(e, 0); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err: return err.code, json.loads(err.read())
    try:
        assert "basic" in call("GET", "/health")[1]["tasksets"]          # the test copies every taskset in the repo
        assert call("GET", "/versions/tree")[0] == 200 and isinstance(call("GET", "/versions/tree")[1]["nodes"], list)
        assert call("GET", "/evolve")[1]["iterations"] == []
        s, ts = call("GET", "/tasksets"); assert s == 200 and ts["tasksets"][0]["n_tasks"] == 10 and ts["tasksets"][0]["groups"]["evolve"] == 7
        assert call("GET", "/tasksets/basic")[1]["tasks"][0]["status"] == "unchecked"
        assert call("GET", "/tasksets/nope")[0] == 404
        s, v = call("POST", "/versions", {"label": "ref", "agent_cmd": ref_cmd(e), "layers": ["L-1"]}); assert s == 201
        assert call("GET", "/versions")[1]["versions"][0]["layers"] == ["L-1"]
        s, b = call("POST", "/batches", {"kind": "adhoc", "taskset": "basic", "version": v["id"], "k": 1, "tasks": ["log-report"]}); assert s == 202
        assert call("POST", "/batches", {"kind": "compare", "taskset": "basic", "version": v["id"]})[0] == 400
        assert call("POST", "/batches", {"kind": "adhoc", "taskset": "basic", "version": "v-missing"})[0] == 404
        s, c = call("POST", "/tasksets/basic/check", {"tasks": ["config-edit"]}); assert s == 202
        wait(e, b["batch_id"]); wait(e, c["batch_id"])
        s, full = call("GET", f"/batches/{b['batch_id']}")
        assert full["status"] == "done" and full["matrix"]["log-report"][0]["score"] == 1.0 and full["stats"]["mean"] == 1.0
        assert call("GET", f"/runs/{full['runs'][0]}")[1]["verify"]["score"] == 1.0
        assert call("GET", "/runs/r-none")[0] == 404
        lst = call("GET", "/batches")[1]["batches"]; assert {x["kind"] for x in lst} == {"adhoc", "check"} and "runs" not in lst[0]
        last = call("GET", "/tasksets/basic")[1]["tasks"][-1]; assert last["id"] == "config-edit" and last["status"] == "ok"
        assert call("DELETE", f"/batches/{b['batch_id']}")[1] == {"cancelled": True}
    finally:
        srv.shutdown(); srv.server_close()


if __name__ == "__main__":  # tiny runner when pytest is not installed
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        t0 = time.time()
        try: fn(); print(f"PASS {fn.__name__} ({time.time() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1; print("FAIL", fn.__name__, repr(e)); traceback.print_exc()
    for d in _TMP: shutil.rmtree(d, ignore_errors=True)
    print(f"{len(fns) - failed}/{len(fns)} passed"); sys.exit(1 if failed else 0)


def test_import_mined_task_writes_it_and_checks_it():
    """docs/007 任务从哪来: a task posted by the Agent service lands in the state directory's task set, lists next to the built-ins,
    and its check passes (reference passes, the untouched workspace fails)."""
    import base64, io, tarfile
    e = make_ev()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("repo-abc/pkg/mod.py", b"def f():\n    return 1\n"), ("repo-abc/pkg/__init__.py", b""), ("repo-abc/tests/test_mod.py", b"stale\n")):
            ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    r = e.import_task("user-o-r", {"id": "t1-make-f-2", "prompt": "make f return 2", "init_tar_gz_b64": base64.b64encode(buf.getvalue()).decode(),
                                   "verify_files": {"tests/test_mod.py": "from pkg.mod import f\ndef test_f(): assert f() == 2\n"}, "reference_files": {"pkg/mod.py": "def f():\n    return 2\n"},
                                   "source": {"repo": "o/r", "pr": 7}})
    assert r["task"] == "t1-make-f-2" and r["n_tasks"] == 1 and r["check_batch"]
    base = os.path.join(e.cfg["state"], "tasksets", "user-o-r", "t1-make-f-2")
    assert os.path.exists(os.path.join(base, "init", "pkg", "mod.py")) and not os.path.exists(os.path.join(base, "init", "tests", "test_mod.py"))   # hidden tests stay out of the workspace
    assert open(os.path.join(base, "verify", "tests.txt")).read().strip() == "tests/test_mod.py"
    names = [t["name"] for t in ev.list_tasksets(ev.taskset_dirs(e.cfg))]; assert "user-o-r" in names and "basic" in names
    v = wait(e, r["check_batch"])
    assert v["status"] == "done" and v["summary"]["all_ok"], json.dumps(v["summary"], indent=1)
