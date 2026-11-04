#!/usr/bin/env python3
"""Mock control-plane + evaluator for developing web/ (工作台) locally.

Usage: python3 web/mock-server.py [port]   (default 127.0.0.1:18799)
Serves web/ at /ui/ and fakes the CONTRACT.md endpoints with in-memory state.
"""
import base64, json, os, random, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
NOW = time.time

# ---------------------------------------------------------------- sandboxes
SANDBOXES = {}
SESSIONS = {}   # sid -> {"sandbox": id, "pty": bool, "out": str, "line": str}
FILES = {}      # sandbox id -> {path: bytes}
LAYERS = [{"id": "L-a1b2c3d4", "name": "aios-base", "bytes": 52428800}]

def new_sandbox(body, fork_of=None):
    sid = "sb-" + uuid.uuid4().hex[:8]
    info = {
        "id": sid, "status": "running",
        "template": body.get("template") or "ubuntu-24.04",
        "class": body.get("class") or "persistent",
        "vcpu": int(body.get("vcpu") or 2), "mem": int(body.get("mem") or 2048),
        "ttl_idle_s": body.get("ttl_idle_s", 1800), "ttl_max_s": body.get("ttl_max_s", 86400),
        "created": NOW(), "node": body.get("node") or random.choice(["node-a", "node-b"]),
        "durable": body.get("class", "persistent") != "ephemeral",
        "boot_mode": "snapshot" if body.get("boot", "auto") != "cold" else "cold",
        "layers": body.get("layers") or [],
        "network": body.get("network") or {"allow_hosts": [], "allow_direct": []},
    }
    if fork_of:
        info["fork_of"] = fork_of
    SANDBOXES[sid] = info
    FILES[sid] = dict(FILES.get(fork_of, {})) if fork_of else {
        "/task/README.md": b"# demo task\n\nEdit src/app.py so tests pass.\n",
        "/task/src/app.py": b"def add(a, b):\n    return a - b  # bug\n",
        "/task/bin/blob": bytes(range(256)),
    }
    return info

for _ in range(3):
    new_sandbox({})
_s = list(SANDBOXES.values())
_s[1]["status"] = "paused"; _s[1]["created"] -= 7200
_s[2]["class"] = "ephemeral"; _s[2]["durable"] = False; _s[2]["boot_mode"] = "cold"; _s[2]["created"] -= 90000
_s[2]["network"] = {"allow_hosts": ["fc-model", "pypi.org", "*.pythonhosted.org"], "allow_direct": []}

NODES = [
    {"name": "node-a", "url": "http://10.0.0.1:8700", "alive": True,
     "health": {"vms_total": 3, "vms_running": 2, "max_vms": 48, "mem_mib_running": 4096,
                "mem_budget_mib": 196608, "template_snapshots": ["ubuntu-24.04", "ubuntu-24.04+L-a1b2c3d4"],
                "pools": {"ubuntu-24.04": {"warm": 2, "target": 2}}}},
    {"name": "node-b", "url": "http://10.0.0.2:8700", "alive": False,
     "health": {"vms_total": 0, "vms_running": 0, "max_vms": 16, "mem_mib_running": 0,
                "mem_budget_mib": 65536, "template_snapshots": [], "pools": {}}},
]

# ---------------------------------------------------------------- eval
TASK_IDS = ["coding-001", "coding-002", "ops-003", "data-004", "text-005", "coding-006"]
TASKSETS = {
    "demo-v1": {
        "name": "demo-v1", "version": "1.0.2",
        "tasks": [
            {"id": t, "domain": t.split("-")[0], "group": ["evolve", "evolve", "validate", "evolve", "test", "evolve"][i],
             "timeout_s": 600,
             "status": ["ok", "ok", "unchecked", "invalid", "ok", "ok"][i],
             "checks": None if i == 2 else {"reference_pass": i != 3, "noop_fail": True,
                                             "repeat_consistent": True, "within_time": True},
             "pass_rate_hist": [0.2, 0.4, 0.6, 1.0, 0.0, 0.8][i]}
            for i, t in enumerate(TASK_IDS)
        ],
    }
}
VERSIONS = [
    {"id": "v-0001", "label": "stable-0", "agent_cmd": "python3 -m aios.run", "layers": ["L-a1b2c3d4"],
     "note": "initial stable", "created": NOW() - 86400 * 3, "parent": None},
    {"id": "v-0002", "label": "cand-ctx-trim", "agent_cmd": "python3 -m aios.run", "layers": ["L-a1b2c3d4", "L-ffee0011"],
     "note": "trim context after 20 steps", "created": NOW() - 86400, "parent": "v-0001"},
]
BATCHES = {}
RUNS = {}

SAMPLE_TRAJ = {
    "info": {"agent": "mini-swe-agent", "version": "1.1", "exit_status": "Submitted"},
    "messages": [
        {"role": "system", "content": "You are a helpful software engineer working in a sandbox. Use bash."},
        {"role": "user", "content": "# demo task\n\nFix `add` in src/app.py so that tests pass. Do not edit tests."},
        {"role": "assistant", "content": "I will first look at the repository layout and the failing function.",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "ls -R /task && cat /task/src/app.py"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "/task:\nREADME.md  src  tests\n\n/task/src:\napp.py\n\n/task/tests:\ntest_app.py\ndef add(a, b):\n    return a - b  # bug\n"},
        {"role": "assistant", "content": "The function subtracts instead of adding. I will fix it and run the tests.",
         "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "sed -i 's/a - b  # bug/a + b/' /task/src/app.py && cd /task && python3 -m pytest -q"})}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "." * 3 + "\n3 passed in 0.02s\n" + "\n".join(f"log line {i}" for i in range(60))},
        {"role": "assistant", "content": "Tests pass. Submitting.",
         "tool_calls": [{"id": "c3", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})}}]},
        {"role": "exit", "content": "Submitted"},
    ],
}

def make_run(batch_id, task, version, k, score=None, infra=False, cheat=None):
    rid = "run-" + uuid.uuid4().hex[:8]
    if score is None:
        score = random.choice([0.0, 1.0, 1.0, 0.5])
    run = {"id": rid, "batch": batch_id, "task": task, "version": version, "k_index": k,
           "score": score, "pass": score >= 1.0, "infra_fail": infra, "attempts": 2 if infra else 1,
           "cost": round(random.uniform(0.01, 0.2), 4), "tokens_in": random.randint(2000, 30000),
           "tokens_out": random.randint(200, 3000), "elapsed_s": round(random.uniform(20, 300), 1),
           "agent_exit": 0, "verify": {"exit": 0 if score > 0 else 1,
                                       "stdout_tail": "collected 3 items\n...\n3 passed\nSCORE %.1f\n" % score, "score": score},
           "cheat": cheat or [], "traj": SAMPLE_TRAJ, "sandbox_id": "sb-deadbeef", "node": "node-a", "created": NOW()}
    RUNS[rid] = run
    return run

def finish_batch(b):
    """Fill matrix + stats for a batch (synchronously; mock)."""
    tasks = [t["id"] for t in TASKSETS[b["taskset"]]["tasks"] if t["status"] == "ok"]
    matrix, per_task = {}, {}
    for t in tasks:
        cells = []
        for k in range(b["k"]):
            cheat = [{"signal": "edited_tests", "detail": "tests/test_app.py modified"}] if (t == "coding-006" and k == 0) else None
            r = make_run(b["id"], t, b["version"], k, infra=(t == "ops-003" and k == 1), cheat=cheat)
            cells.append({"run_id": r["id"], "score": r["score"], "pass": r["pass"], "infra_fail": r["infra_fail"],
                          "cost": r["cost"], "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                          "elapsed_s": r["elapsed_s"], "cheat": r["cheat"]})
        matrix[t] = cells
        ok = [c for c in cells if not c["infra_fail"]]
        p = sum(c["pass"] for c in ok) / max(1, len(ok))
        per_task[t] = {"p_hat": round(p, 3), "var": round(p * (1 - p), 3)}
    mean = round(sum(v["p_hat"] for v in per_task.values()) / max(1, len(per_task)), 3)
    stats = {"mean": mean, "avg_k": b["k"], "per_task": per_task, "delta": 0.08}
    if b["kind"] == "compare" and b.get("base_batch") in BATCHES:
        base = BATCHES[b["base_batch"]]["stats"]
        diffs = [per_task[t]["p_hat"] - base["per_task"].get(t, {}).get("p_hat", 0) for t in per_task]
        diff = round(sum(diffs) / max(1, len(diffs)), 3)
        stats["paired"] = {"diff": diff, "ci95": [round(diff - 0.1, 3), round(diff + 0.1, 3)], "mcnemar_p": 0.21, "n_tasks": len(diffs)}
        stats["accept"] = diff > stats["delta"] and diff - 0.1 > 0
        stats["reason"] = "配对提升超过噪声带且 CI 下界为正" if stats["accept"] else "配对提升未超过噪声带 δ=0.08 或 CI 下界非正"
    b["runs"] = [c["run_id"] for cells in matrix.values() for c in cells]
    b["matrix"] = matrix
    b["stats"] = stats
    b["status"] = "done"
    total = len(tasks) * b["k"]
    b["progress"] = {"done": total, "total": total, "infra_fail": sum(c["infra_fail"] for cells in matrix.values() for c in cells)}
    b["summary"] = {"mean": mean, "delta": stats["delta"]}
    if "paired" in stats:
        b["summary"].update({"diff": stats["paired"]["diff"], "accept": stats["accept"]})

def new_batch(body):
    bid = "b-" + uuid.uuid4().hex[:8]
    b = {"id": bid, "kind": body.get("kind", "baseline"), "taskset": body.get("taskset", "demo-v1"),
         "version": body.get("version", "v-0001"), "k": int(body.get("k") or 5), "label": body.get("label", ""),
         "status": "queued", "created": NOW(), "progress": {"done": 0, "total": 0, "infra_fail": 0},
         "base_batch": body.get("base_batch"), "cascade": body.get("cascade")}
    BATCHES[bid] = b
    return b

def progress_batches():
    """Advance queued/running batches a bit on each poll so the UI shows movement."""
    for b in BATCHES.values():
        if b["status"] == "queued":
            b["status"] = "running"
            n = len([t for t in TASKSETS[b["taskset"]]["tasks"] if t["status"] == "ok"]) * b["k"]
            b["progress"] = {"done": 0, "total": n, "infra_fail": 0}
        elif b["status"] == "running":
            b["progress"]["done"] = min(b["progress"]["total"], b["progress"]["done"] + max(1, b["progress"]["total"] // 4))
            if b["progress"]["done"] >= b["progress"]["total"]:
                finish_batch(b)

_b0 = new_batch({"kind": "baseline", "version": "v-0001", "k": 3, "label": "baseline stable-0"}); finish_batch(_b0)
_b1 = new_batch({"kind": "compare", "version": "v-0002", "k": 3, "base_batch": _b0["id"], "label": "ctx-trim vs stable"}); finish_batch(_b1)
_b2 = new_batch({"kind": "check", "version": "v-0001", "k": 1, "label": "import check"})

# ---------------------------------------------------------------- handler
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    # helpers
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _jbody(self):
        raw = self._body()
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _static(self, rel):
        if rel in ("", "/"):
            rel = "index.html"
        p = os.path.normpath(os.path.join(WEB_DIR, rel.lstrip("/")))
        if not p.startswith(WEB_DIR) or not os.path.isfile(p):
            return self._err(404, "not found")
        ct = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8"}.get(os.path.splitext(p)[1], "application/octet-stream")
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        if p == "/" :
            self.send_response(302); self.send_header("Location", "/ui/"); self.send_header("Content-Length", "0"); self.end_headers(); return
        if p.startswith("/ui"):
            return self._static(p[3:])
        if p == "/favicon.ico":
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
        with LOCK:
            if p == "/health":
                return self._json({"ok": True, "nodes": {n["name"]: {"alive": n["alive"]} for n in NODES}, "routes": {}})
            if p == "/nodes":
                return self._json({"nodes": NODES})
            if p == "/sandboxes":
                return self._json({"sandboxes": sorted(SANDBOXES.values(), key=lambda s: -s["created"])})
            parts = p.strip("/").split("/")
            if parts[0] == "sandboxes" and len(parts) >= 2:
                sb = SANDBOXES.get(parts[1])
                if not sb: return self._err(404, "no such sandbox")
                if len(parts) == 2: return self._json(sb)
                if parts[2] == "files":
                    path = (q.get("path") or [""])[0]
                    data = FILES[sb["id"]].get(path)
                    if data is None: return self._err(404, "no such file: " + path)
                    self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if parts[0] == "eval":
                return self._eval_get(parts[1:])
        self._err(404, "not found")

    def _eval_get(self, parts):
        if parts == ["health"]:
            return self._json({"ok": True, "cp": True, "versions": len(VERSIONS), "tasksets": list(TASKSETS)})
        if parts == ["tasksets"]:
            return self._json({"tasksets": [{"name": t["name"], "version": t["version"], "n_tasks": len(t["tasks"]),
                                             "groups": {g: sum(1 for x in t["tasks"] if x["group"] == g) for g in ("evolve", "validate", "test")},
                                             "checked": all(x["status"] != "unchecked" for x in t["tasks"])} for t in TASKSETS.values()]})
        if parts[0] == "tasksets" and len(parts) == 2:
            t = TASKSETS.get(parts[1]); return self._json(t) if t else self._err(404, "no such taskset")
        if parts == ["versions"]:
            return self._json({"versions": VERSIONS})
        if parts == ["batches"]:
            progress_batches()
            return self._json({"batches": [{k: v for k, v in b.items() if k not in ("matrix", "runs", "stats")} for b in sorted(BATCHES.values(), key=lambda b: -b["created"])]})
        if parts[0] == "batches" and len(parts) == 2:
            progress_batches()
            b = BATCHES.get(parts[1]); return self._json(b) if b else self._err(404, "no such batch")
        if parts[0] == "runs" and len(parts) == 2:
            r = RUNS.get(parts[1]); return self._json(r) if r else self._err(404, "no such run")
        self._err(404, "not found")

    def do_PUT(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query); parts = p.strip("/").split("/")
        with LOCK:
            if parts[0] == "sandboxes" and len(parts) == 3:
                sb = SANDBOXES.get(parts[1])
                if not sb: return self._err(404, "no such sandbox")
                if parts[2] == "files":
                    path = (q.get("path") or [""])[0]
                    data = self._body(); FILES[sb["id"]][path] = data
                    return self._json({"bytes": len(data)})
                if parts[2] == "network":
                    body = self._jbody()
                    sb["network"] = {"allow_hosts": body.get("allow_hosts", sb["network"].get("allow_hosts", [])),
                                     "allow_direct": body.get("allow_direct", sb["network"].get("allow_direct", []))}
                    return self._json({"network": sb["network"]})
        self._err(404, "not found")

    def do_DELETE(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        with LOCK:
            if parts[0] == "sandboxes" and len(parts) == 2 and parts[1] in SANDBOXES:
                del SANDBOXES[parts[1]]; FILES.pop(parts[1], None); return self._json({"deleted": parts[1]})
            if parts[0] == "sandboxes" and len(parts) == 4 and parts[2] == "sessions":
                SESSIONS.pop(parts[3], None); return self._json({"deleted": parts[3]})
            if parts[:2] == ["eval", "batches"] and len(parts) == 3 and parts[2] in BATCHES:
                BATCHES[parts[2]]["status"] = "failed"; return self._json({"cancelled": parts[2]})
        self._err(404, "not found")

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        body = self._jbody()
        if parts[0] == "sandboxes":
            if len(parts) == 1:
                with LOCK:
                    return self._json(new_sandbox(body), 201)
            with LOCK:
                sb = SANDBOXES.get(parts[1])
            if not sb: return self._err(404, "no such sandbox")
            act = parts[2] if len(parts) > 2 else ""
            if act == "pause":
                sb["status"] = "paused"; return self._json(sb)
            if act == "resume":
                sb["status"] = "running"; return self._json(sb)
            if act == "exec":
                if len(parts) == 4 and parts[3] == "stream":
                    return self._exec_stream(body)
                cmd = body.get("cmd", "")
                return self._json({"exit": 0, "stdout": "$ %s\n(mock output for user=%s)\n" % (cmd, body.get("user", "agent")),
                                   "stderr": "", "timeout": False, "elapsed_s": 0.12})
            if act == "sessions":
                with LOCK:
                    if len(parts) == 3:
                        s = "s-" + uuid.uuid4().hex[:6]
                        SESSIONS[s] = {"sandbox": sb["id"], "pty": bool(body.get("pty")), "out": "agent@%s:~$ " % sb["id"], "line": ""}
                        return self._json({"session": s, "pty": SESSIONS[s]["pty"]}, 201)
                    s = SESSIONS.get(parts[3])
                    if not s: return self._err(404, "no such session")
                    sub = parts[4] if len(parts) > 4 else ""
                    if sub == "write":
                        data = base64.b64decode(body.get("data_b64", "")).decode("utf-8", "replace")
                        for ch in data:
                            if ch in "\r\n":
                                line = s["line"]; s["line"] = ""
                                s["out"] += "\r\n" + ("" if not line.strip() else "mock-sh: ran `%s`\r\n" % line) + "agent@%s:~$ " % sb["id"]
                            else:
                                s["line"] += ch; s["out"] += ch
                        return self._json({"bytes": len(data)})
                    if sub == "exec":
                        return self._json({"exit": 0, "stdout": "(session exec) %s\n" % body.get("cmd", ""), "stderr": ""})
                    if sub == "read":
                        pass
                if sub == "read":
                    wait = float(body.get("wait", 0) or 0)
                    deadline = NOW() + min(wait, 5)
                    while True:
                        with LOCK:
                            out = s["out"]; s["out"] = ""
                        if out or NOW() >= deadline:
                            break
                        time.sleep(0.05)
                    return self._json({"data_b64": base64.b64encode(out.encode()).decode(), "bytes": len(out)})
            if act == "fork":
                with LOCK:
                    clones = [new_sandbox({"class": body.get("class", "ephemeral"), "template": sb["template"], "vcpu": sb["vcpu"],
                                           "mem": sb["mem"], "node": sb["node"], "layers": sb["layers"]}, fork_of=sb["id"])
                              for _ in range(int(body.get("n") or 1))]
                return self._json({"clones": clones}, 201)
            if act == "pack":
                layer = {"id": "L-" + uuid.uuid4().hex[:8], "name": body.get("name") or "layer", "bytes": random.randint(10**6, 10**8), "created": NOW()}
                LAYERS.append(layer); return self._json(layer, 201)
            if act in ("migrate", "export"):
                return self._json({"ok": True})
            return self._err(404, "not found")
        if parts[0] == "eval":
            with LOCK:
                ev = parts[1:]
                if ev[0] == "tasksets" and len(ev) == 3 and ev[2] == "check":
                    t = TASKSETS.get(ev[1])
                    if not t: return self._err(404, "no such taskset")
                    for x in t["tasks"]:
                        if x["status"] == "unchecked":
                            x["status"] = "ok"; x["checks"] = {"reference_pass": True, "noop_fail": True, "repeat_consistent": True, "within_time": True}
                    b = new_batch({"kind": "check", "taskset": ev[1], "version": "v-0001", "k": 1, "label": "check " + ev[1]})
                    return self._json({"batch_id": b["id"]})
                if ev == ["versions"]:
                    v = {"id": "v-%04d" % (len(VERSIONS) + 1), "label": body.get("label", ""), "agent_cmd": body.get("agent_cmd") or "python3 -m aios.run",
                         "layers": body.get("layers") or [], "note": body.get("note", ""), "created": NOW(), "parent": body.get("parent") or None}
                    VERSIONS.append(v); return self._json({"id": v["id"]}, 201)
                if ev == ["batches"]:
                    b = new_batch(body); return self._json({"batch_id": b["id"]}, 202)
        self._err(404, "not found")

    def _exec_stream(self, body):
        cmd = body.get("cmd", "")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        def chunk(obj):
            data = (json.dumps(obj) + "\n").encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n"); self.wfile.flush()
        t0 = NOW()
        chunk({"event": "stdout", "data": "$ %s\n" % cmd})
        for i in range(6):
            time.sleep(0.25)
            chunk({"event": "stdout" if i != 3 else "stderr", "data": "line %d of mock output (user=%s)\n" % (i + 1, body.get("user", "agent"))})
        chunk({"done": True, "exit": 0, "timeout": False, "elapsed_s": round(NOW() - t0, 2)})
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18799
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    print("mock server on http://127.0.0.1:%d/ui/" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
