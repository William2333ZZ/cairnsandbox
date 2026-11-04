#!/usr/bin/env python3
"""Network / streaming / recovery tests for the node agent. usage: test-net.py http://host:port [restart-cmd]"""
import json, subprocess, sys, time, urllib.request, uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
AUTH = {"Authorization": "Bearer " + __import__("os").environ["NODE_TOKEN"]} if __import__("os").environ.get("NODE_TOKEN") else {}
RESTART = sys.argv[2] if len(sys.argv) > 2 else None

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=180) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]
# guest-side fetch through whatever proxy env the daemon injected; prints http status or an error class
FETCH = ("f(){ if command -v curl >/dev/null; then for i in 1 2 3; do c=$(curl -s -o /dev/null -m 15 -w '%{http_code}' \"$1\") && { echo \"$c\"; return; }; sleep 1; done; echo \"$c ERR\"; "     # three tries: the public host behind the proxy is not ours, one reset must not fail the suite
         "else python3 -c 'import sys,urllib.request as u; print(u.urlopen(sys.argv[1],timeout=15).status)' \"$1\" 2>&1 | tail -1 | grep -o '[0-9]\\{3\\}\\|[A-Za-z]*Error' | tail -1; fi; }; f ")

BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}   # the node is shared: only sandboxes that appear during this run count as left behind
print("== default deny ==")
c, a = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 120}); A = a["id"]
step("create with ip", c == 201 and a["ip"], f"{A} ip={a['ip']}")
r = ex(A, "ip -4 -br addr show eth0; ip route | head -2; env | grep -i proxy | head -2")
print("   guest net:", " | ".join(r["stdout"].split("\n")[:4]))
r = ex(A, FETCH + "http://mirrors.aliyun.com/pypi/simple/")
step("deny-all policy -> proxy refuses (403)", "403" in r["stdout"], r["stdout"].strip())

print("== allow a host ==")
req("PUT", f"/sandboxes/{A}/network", {"allow_hosts": ["mirrors.aliyun.com"]})
r = ex(A, FETCH + "https://mirrors.aliyun.com/pypi/simple/")
step("https via CONNECT to allowed host", r["stdout"].strip() == "200", r["stdout"].strip())
r = ex(A, FETCH + "http://mirrors.aliyun.com/ubuntu/")
step("plain http via proxy to allowed host", r["stdout"].strip().startswith("200"), r["stdout"].strip())
r = ex(A, FETCH + "https://pypi.org/simple/")
step("non-allowed host still blocked", "200" not in r["stdout"], r["stdout"].strip())
r = ex(A, "timeout 4 bash -c 'exec 3<>/dev/tcp/140.205.220.96/80' 2>/dev/null && echo CONNECTED || echo blocked")
step("direct (non-proxy) egress dropped by nftables", "CONNECTED" not in r["stdout"], r["stdout"].strip())
r = ex(A, "if command -v curl >/dev/null; then curl -s -m 20 -w '%{size_download} bytes %{time_total} s\\n' -o /dev/null https://mirrors.aliyun.com/pypi/simple/six/; else python3 -c \"import urllib.request,time; t=time.time(); d=urllib.request.urlopen('https://mirrors.aliyun.com/pypi/simple/six/',timeout=20).read(); print(len(d),'bytes', round(time.time()-t,2),'s')\"; fi")
step("real download through proxy", "bytes" in r["stdout"], r["stdout"].strip())

print("== fork: clones get their own IP and keep egress ==")
c, f = req("POST", f"/sandboxes/{A}/fork", {"n": 2, "ttl_idle_s": 120})
if "clones" not in f: print("fork error:", c, f); sys.exit(1)
clones = [x["id"] for x in f["clones"]]
ips = {A: a["ip"], **{x["id"]: x["ip"] for x in f["clones"]}}
gips = {sid: ex(sid, "ip -4 -o addr show eth0 | awk '{print $4}'")["stdout"].strip().split("/")[0] for sid in ips}
step("distinct IPs, guest matches node", len(set(ips.values())) == 3 and gips == ips, str(gips))
rs = {sid: ex(sid, FETCH + "https://mirrors.aliyun.com/pypi/simple/")["stdout"].strip() for sid in ips}
step("parent and clones all reach allowed host", all(v == "200" for v in rs.values()), str(rs))

print("== pause/resume keeps network ==")
req("POST", f"/sandboxes/{A}/pause")
r = ex(A, FETCH + "https://mirrors.aliyun.com/pypi/simple/")
step("after transparent resume, egress works", r["stdout"].strip() == "200", r["stdout"].strip())

print("== streaming exec ==")
rq = urllib.request.Request(BASE + f"/sandboxes/{A}/exec/stream", data=json.dumps({"cmd": "for i in 1 2 3; do echo t$i; sleep 0.3; done"}).encode(), method="POST", headers={"Content-Type": "application/json", **AUTH})
t0 = time.time(); got = []
with urllib.request.urlopen(rq, timeout=60) as resp:
    for line in resp:
        got.append((round(time.time() - t0, 2), json.loads(line)))
step("chunks arrive incrementally", len(got) >= 4 and got[-1][1].get("done") and got[2][0] - got[0][0] >= 0.4, str([(t, e.get("data", e.get("exit"))) for t, e in got]))

print("== audit shows egress decisions ==")
c, au = req("GET", "/audit?n=200"); eg = [e for e in au["events"] if e["op"] == "egress"]
step("egress audited with allow/deny", any(e["allowed"] for e in eg) and any(not e["allowed"] for e in eg), f"{len(eg)} egress events")

if RESTART:
    print("== restart recovery ==")
    req("POST", f"/sandboxes/{A}/pause")
    for sid in clones: req("DELETE", f"/sandboxes/{sid}")
    ex_before = ex  # noqa
    subprocess.run(RESTART, shell=True, check=True); time.sleep(2)
    for _ in range(30):
        try: c, lst = req("GET", "/sandboxes"); break
        except Exception: time.sleep(0.5)
    st = {x["id"]: x["status"] for x in lst["sandboxes"]}
    step("paused sandbox recovered after restart", st.get(A) == "paused", str(st))
    r = ex(A, "cat /home/agent/marker 2>/dev/null || echo none; " + FETCH + "https://mirrors.aliyun.com/pypi/simple/")
    step("recovered sandbox resumes with network", "200" in r["stdout"], r["stdout"].strip())
    c, au = req("GET", "/audit?n=30"); print("   ", [e["op"] for e in au["events"] if e["op"] in ("recover", "orphan_removed", "auto_resume")])

print("== cleanup ==")
for sid in [A] + clones: req("DELETE", f"/sandboxes/{sid}")
c, sb_ = req("GET", "/sandboxes"); left_ = [x["id"] for x in sb_.get("sandboxes", []) if x["id"] not in BEFORE]; step("all gone", c == 200 and not left_, f"{len(left_)} left {left_[:3]}")
