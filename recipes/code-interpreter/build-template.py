#!/usr/bin/env python3
"""Build the `code-interpreter-v1` template on a running Cairn stack (docs/guide/code-interpreter.md). Standard library only.

  python3 recipes/code-interpreter/build-template.py --cp http://127.0.0.1:18700 --token $(cat deploy/secrets/cp.token) --layer <agent-langs layer id> [--name code-interpreter-v1]

It boots a build sandbox on the base template with the agent-langs layer (CPython 3.13 and bun under /aios/runtime), puts server.py in it, starts the server, checks that it answers a
cell, and commits the running sandbox as a template: memory and disk, so a sandbox started from the template already has the server (and its warm Python kernel) listening on 49999.
A template lives on the node that built it (placement prefers a node that has the template); use --node to choose it. The agent-langs layer comes from recipes/agent-langs (node/build-layer.sh); see the guide for the files it needs.
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--cp", required=True); ap.add_argument("--token", required=True); ap.add_argument("--layer", required=True)
ap.add_argument("--name", default="code-interpreter-v1"); ap.add_argument("--template", default="debian-13"); ap.add_argument("--project", default="")
ap.add_argument("--node", default="", help="build on this node (layers and templates live on the node that built them)")
ap.add_argument("--mem", type=int, default=1024); ap.add_argument("--vcpu", type=int, default=2)
a = ap.parse_args()
HERE = os.path.dirname(os.path.abspath(__file__)); PY = "/aios/runtime/python/bin/python3"


def call(method, path, body=None, raw=None, timeout=900):
    h = {"Authorization": "Bearer " + a.token}
    if a.project: h["X-Project"] = a.project
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if raw is None and body is not None: h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(a.cp.rstrip("/") + path, data=data, method=method, headers=h), timeout=timeout) as r:
            t = r.read(); return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        t = e.read(); return e.code, (json.loads(t) if t[:1] in (b"{", b"[") else {"error": t.decode()[:300]})


def ex(sid, cmd, user="agent", timeout=120):
    st, r = call("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, "user": user, "timeout": timeout}); assert st == 200, (st, r); return r


spec = {"template": a.template, "class": "persistent", "mem": a.mem, "vcpu": a.vcpu, "disk": 2048, "layers": [a.layer], "ttl_idle_s": 1800}
if a.node: spec["node"] = a.node
st, sb = call("POST", "/sandboxes", spec)
assert st == 201, (st, sb); sid = sb["id"]; print("build sandbox", sid, file=sys.stderr)
try:
    r = ex(sid, f"{PY} --version && /aios/runtime/bun/bun --version"); assert r["exit"] == 0, r; print(r["stdout"].strip().replace("\n", " | "), file=sys.stderr)
    st, _ = call("PUT", f"/sandboxes/{sid}/files?path=/opt/ci/server.py", raw=open(os.path.join(HERE, "server.py"), "rb").read()); assert st == 200, st
    ex(sid, "chmod -R a+rX /opt/ci", user="root")
    ex(sid, f"cd /home/agent && setsid nohup {PY} /opt/ci/server.py > /tmp/ci.log 2>&1 < /dev/null &")
    probe = f"{PY} -c \"import urllib.request as u;print(u.urlopen('http://127.0.0.1:49999/health',timeout=2).read().decode())\""
    for _ in range(60):
        r = ex(sid, probe)
        if r["exit"] == 0 and "OK" in r["stdout"]: break
        time.sleep(0.5)
    else: sys.exit("the server did not come up: " + ex(sid, "cat /tmp/ci.log")["stdout"])
    run = f"{PY} - <<'EOF'\nimport json,urllib.request as u\nr=u.Request('http://127.0.0.1:49999/execute',json.dumps({{'code':'sum(range(10))'}}).encode(),{{'Content-Type':'application/json'}})\nprint(u.urlopen(r,timeout=30).read().decode())\nEOF"
    out = ex(sid, run)["stdout"]; assert '"text": "45"' in out, out; print("warm cell ok", file=sys.stderr)
    st, b = call("POST", "/builds", {"kind": "commit", "sandbox": sid, "name": a.name, "note": "code-interpreter server running"}); assert st == 201, (st, b)
    for _ in range(600):
        st, d = call("GET", f"/builds/{b['id']}")
        if d.get("status") != "running": break
        time.sleep(1)
    assert d.get("status") == "done", d; print(f"template {a.name} ready: {json.dumps(d.get('result'))[:200]}", file=sys.stderr)
finally:
    call("DELETE", f"/sandboxes/{sid}")
