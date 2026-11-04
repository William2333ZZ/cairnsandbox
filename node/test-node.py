#!/usr/bin/env python3
"""End-to-end test of the node agent over HTTP. usage: test-node.py http://host:port"""
import base64, json, sys, time, urllib.request, uuid
KEY = uuid.uuid4().hex

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
AUTH = {"Authorization": "Bearer " + __import__("os").environ["NODE_TOKEN"]} if __import__("os").environ.get("NODE_TOKEN") else {}

def req(method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH, **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            b = resp.read(); return resp.status, (json.loads(b) if resp.headers.get("Content-Type", "").startswith("application/json") else b)
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else b)

import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes

BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}   # the node is shared: only sandboxes that appear during this run count as left behind
print("== health =="); c, h = req("GET", "/health"); print(h)

print("== create 2 sandboxes (idempotent) ==")
t0 = time.time(); c, a = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 4}, headers={"Idempotency-Key": KEY})
step("create A", c == 201, f"{a['id']} {a['status']} in {time.time()-t0:.2f}s")
c2, a2 = req("POST", "/sandboxes", {"class": "persistent"}, headers={"Idempotency-Key": KEY})
step("idempotent replay returns same id", a2["id"] == a["id"])
c, b = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 3, "env": {"FOO": "bar baz"}}); step("create B (ephemeral)", c == 201, b["id"])
r = req("POST", f"/sandboxes/{b['id']}/exec", {"cmd": "echo [$FOO] [$BAR]", "env": {"BAR": "q"}})[1]; step("the sandbox's env is under every exec, the request's on top", r.get("stdout", "").strip() == "[bar baz] [q]", r.get("stdout", "").strip())

print("== exec / files / sessions ==")
c, r = req("POST", f"/sandboxes/{a['id']}/exec", {"cmd": "id -un; echo hello > /home/agent/f.txt; cat /home/agent/f.txt"})
step("exec as agent", r["exit"] == 0 and r["stdout"].split() == ["agent", "hello"], r["stdout"].strip().replace("\n", " "))
c, r = req("PUT", f"/sandboxes/{a['id']}/files?path=/home/agent/up.bin", raw=b"\x00\x01binary\xff")
c, data = req("GET", f"/sandboxes/{a['id']}/files?path=/home/agent/up.bin"); step("file round trip", data == b"\x00\x01binary\xff")
c, s = req("POST", f"/sandboxes/{a['id']}/sessions", {}); sid = s["session"]
req("POST", f"/sandboxes/{a['id']}/sessions/{sid}/exec", {"cmd": "cd /tmp; export X=1"})
c, r = req("POST", f"/sandboxes/{a['id']}/sessions/{sid}/exec", {"cmd": "pwd; echo $X"}); step("session state persists", r["stdout"].split() == ["/tmp", "1"])
req("DELETE", f"/sandboxes/{a['id']}/sessions/{sid}")

print("== pause -> transparent resume ==")
t0 = time.time(); c, r = req("POST", f"/sandboxes/{a['id']}/pause"); step("pause", r["status"] == "paused", f"{time.time()-t0:.2f}s")
t0 = time.time(); c, r = req("POST", f"/sandboxes/{a['id']}/exec", {"cmd": "cat /home/agent/f.txt"})
step("exec on paused sandbox auto-resumes, state kept", r["stdout"].strip() == "hello", f"{time.time()-t0:.2f}s")

print("== fork ==")
t0 = time.time(); c, f = req("POST", f"/sandboxes/{a['id']}/fork", {"n": 3, "ttl_idle_s": 30})
step("fork x3", c == 201 and len(f["clones"]) == 3, f"{f.get('s')}s")
outs = [req("POST", f"/sandboxes/{x['id']}/exec", {"cmd": f"cat /home/agent/f.txt; echo {i} > /home/agent/me; hostname"})[1]["stdout"].split() for i, x in enumerate(f["clones"])]
step("clones inherit parent file and are independent", all(o[0] == "hello" for o in outs), str(outs))
c, r = req("POST", f"/sandboxes/{a['id']}/exec", {"cmd": "ls /home/agent/me 2>&1 | head -c 120"}); step("parent unaffected by clone writes", "No such file" in r["stdout"], "")

print("== admission ==")
c, r = req("POST", "/sandboxes", {"mem": 10**9}); step("over memory budget rejected with 429", c == 429, r.get("error"))

print("== TTL: idle 3-4s -> ephemeral destroyed, persistent paused ==")
time.sleep(6)
c, lst = req("GET", "/sandboxes"); st = {x["id"]: x["status"] for x in lst["sandboxes"]}
step("ephemeral B destroyed by ttl", b["id"] not in st, str(st.get(b["id"])))
step("persistent A auto-paused by ttl", st.get(a["id"]) == "paused", str(st.get(a["id"])))

print("== cleanup + audit ==")
for x in f["clones"]: req("DELETE", f"/sandboxes/{x['id']}")
req("DELETE", f"/sandboxes/{a['id']}")
c, au = req("GET", "/audit?n=12"); print("\n".join(json.dumps(e, ensure_ascii=False)[:150] for e in au["events"]))
c, sb_ = req("GET", "/sandboxes"); left_ = [x["id"] for x in sb_.get("sandboxes", []) if x["id"] not in BEFORE]; step("all gone", c == 200 and not left_, f"{len(left_)} left {left_[:3]}")
