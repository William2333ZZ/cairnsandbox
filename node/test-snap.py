#!/usr/bin/env python3
"""Template-snapshot boot, Diff snapshot chains, and SIGTERM drain. usage: test-snap.py http://host:port [restart-cmd]
Light: at most three VMs alive at once."""
import json, os, subprocess, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
RESTART = sys.argv[2] if len(sys.argv) > 2 else None
RESTART_NOJAIL = sys.argv[3] if len(sys.argv) > 3 else None     # restart the node unjailed to exercise the uffd path
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
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]

BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}   # the node is shared: only sandboxes that appear during this run count as left behind
print("== template snapshot ==")
t0 = time.time(); c, tpl = req("POST", "/templates/snapshot", {"mem": 512, "vcpu": 2})
step("build template snapshot", c in (200, 201), f"key={tpl.get('key')} mem={tpl.get('mem_mib')}MiB upper={tpl.get('upper_mib')}MiB boot={tpl.get('boot_s')}s in {time.time()-t0:.1f}s")
step("template memory is a sparse Diff-since-boot file (< 512 MiB)", tpl.get("mem_mib", 1e9) < 512, f"{tpl.get('mem_mib')} MiB")
c, r = req("POST", "/templates/snapshot", {"mem": 512, "vcpu": 2}); step("same spec reuses the existing snapshot", c == 200 and r["key"] == tpl["key"])
c, lst = req("GET", "/templates"); step("listed", any(s["key"] == tpl["key"] for s in lst["snapshots"]))

print("== boot from snapshot vs cold ==")
t0 = time.time(); c, a = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300, "mem": 512, "boot": "snapshot"}); ta = time.time() - t0
step("create from template snapshot", c == 201 and a["boot_mode"] == "snapshot", f"{a.get('id')} {ta:.2f}s chain={a.get('mem_chain')}")
t0 = time.time(); c, b = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 300, "mem": 512, "boot": "cold"}); tb = time.time() - t0
step("cold boot still works", c == 201 and b["boot_mode"] == "cold", f"{b.get('id')} {tb:.2f}s")
step("snapshot boot faster than cold boot", ta < tb, f"{ta:.2f}s vs {tb:.2f}s")
r = ex(a["id"], "hostname; id -un; date +%s; cat /proc/uptime | cut -d' ' -f1; ls /run/sandboxd 2>/dev/null | head -1")
out = r["stdout"].split()
step("guest identity fixed up (hostname = id, clock within 5s, agent user)", out[0] == a["id"] and out[1] == "agent" and abs(int(out[2]) - int(time.time())) < 5, " ".join(out[:4]))
c, r2 = req("POST", "/sandboxes", {"class": "ephemeral", "mem": 256, "boot": "snapshot"}); step("boot=snapshot without a matching template -> 400", c == 400, r2.get("error", "")[:60])
r = ex(a["id"], "if command -v curl >/dev/null; then curl -s -o /dev/null -m 10 -w '%{http_code}' http://mirrors.aliyun.com/ || echo ERR; else python3 -c \"import urllib.request\ntry: print(urllib.request.urlopen('http://mirrors.aliyun.com/',timeout=10).status)\nexcept Exception as e: print(getattr(e,'code','') or type(e).__name__)\"; fi")
step("network pushed after template restore (proxy answers, policy denies)", "403" in r["stdout"], r["stdout"].strip())
req("DELETE", f"/sandboxes/{b['id']}")

print("== Diff snapshot chain on pause/resume ==")
ex(a["id"], "echo v1 > /home/agent/state; x=$(head -c 64M /dev/zero | tr '\\0' a); unset x")
t0 = time.time(); c, p = req("POST", f"/sandboxes/{a['id']}/pause"); tp = time.time() - t0
own = [l for l in p["mem_chain"][1:]]
step("pause writes a Diff layer on top of the template", p["status"] == "paused" and len(p["mem_chain"]) == 2, f"{tp:.2f}s snapshot={p['snapshot_s']}s chain={p['mem_chain']}")
step("Diff layer is small (< 256 MiB for a 512 MiB guest)", own and own[0]["allocated_mib"] < 256, f"{own[0]['allocated_mib'] if own else None} MiB")
t0 = time.time(); r = ex(a["id"], "cat /home/agent/state; echo v2 >> /home/agent/state"); tr = time.time() - t0
c, i = req("GET", f"/sandboxes/{a['id']}")
step("resume keeps state", r["stdout"].split() == ["v1"], f"{tr:.2f}s merge={i['merge_s']}s")
c, h = req("GET", "/health"); u = i.get("uffd") or {}
def uffd_ok(i, u): return u.get("layers") == 2 and i["merge_s"] == 0 and u.get("faults", 0) > 0 and not u.get("error")
def uffd_detail(i, u): return f"faults={u.get('faults')} copied={u.get('pages_copied')}p zero={u.get('pages_zero')}p removes={u.get('removes')} populated={u.get('populated_mib')}MiB serve={u.get('serve_s')}s err={u.get('error')}"
if h.get("uffd"): step("chain served on demand by the uffd handler, no merge file", uffd_ok(i, u), uffd_detail(i, u))
else: step("uffd unavailable here -> chain merged into a file (fallback)", i["merge_s"] > 0 and not u, f"merge={i['merge_s']}s ({h.get('uffd_note')})")
for k in range(3):
    req("POST", f"/sandboxes/{a['id']}/pause"); ex(a["id"], f"echo v{k+3} >> /home/agent/state")
c, i = req("GET", f"/sandboxes/{a['id']}")
step("chain squashed after more than 3 own layers", len(i["mem_chain"]) <= 2, f"chain={i['mem_chain']}")
r = ex(a["id"], "cat /home/agent/state | tr '\\n' ' '")
step("state intact across 4 pause/resume cycles", r["stdout"].split() == ["v1", "v2", "v3", "v4", "v5"], r["stdout"].strip())

print("== fork from a snapshot-booted sandbox ==")
c, f = req("POST", f"/sandboxes/{a['id']}/fork", {"n": 1, "ttl_idle_s": 120})
cl = f["clones"][0]["id"] if c == 201 else None
r = ex(cl, "cat /home/agent/state | tail -1; hostname") if cl else {"stdout": ""}
step("clone inherits state and gets its own hostname", cl and r["stdout"].split() == ["v5", cl], r["stdout"].strip().replace("\n", " "))
c, i = req("GET", f"/sandboxes/{a['id']}"); step("fork squashed parent chain to one sparse file (< 512 MiB)", len(i["mem_chain"]) == 1 and i["mem_chain"][0]["allocated_mib"] < 512, str(i["mem_chain"]))
if cl: req("DELETE", f"/sandboxes/{cl}")

print("== template pool ==")
c, pl = req("PUT", "/templates/pool", {"mem": 512, "vcpu": 2, "ready": 1})
step("pool registered", c == 200 and pl["want"] == 1, str(pl))
for _ in range(60):
    c, pls = req("GET", "/templates/pool"); pi = next((x for x in pls["pools"] if x["key"] == tpl["key"]), None)
    if pi and pi["ready"] >= 1: break
    time.sleep(0.5)
step("pool filled in the background", pi and pi["ready"] == 1, str(pi))
c, h = req("GET", "/health"); step("pooled VM counted in node usage", h.get("vms_pooled") == 1 and h["vms_running"] >= 2, f"running={h['vms_running']} pooled={h.get('vms_pooled')}")
t0 = time.time(); c, pz = req("POST", "/sandboxes", {"class": "persistent", "mem": 512, "ttl_idle_s": 300, "qos": "normal", "network": {"allow_hosts": ["mirrors.aliyun.com"]}}); tz = time.time() - t0
step("create is served from the pool", c == 201 and pz["boot_mode"] == "pool", f"{pz.get('id')} {tz:.3f}s")
r = ex(pz["id"], "date +%s; echo; if command -v curl >/dev/null; then curl -s -o /dev/null -m 10 -w '%{http_code}' http://mirrors.aliyun.com/ubuntu/ || echo ERR; else python3 -c \"import urllib.request\ntry: print(urllib.request.urlopen('http://mirrors.aliyun.com/ubuntu/',timeout=10).status)\nexcept Exception as e: print(getattr(e,'code','') or type(e).__name__)\"; fi")
out = r["stdout"].split()
step("adopted sandbox got the caller's network policy and a fresh clock", len(out) >= 2 and out[1] == "200" and abs(int(out[0]) - int(time.time())) < 5, " ".join(out))
for _ in range(60):
    c, pls = req("GET", "/templates/pool"); pi = next((x for x in pls["pools"] if x["key"] == tpl["key"]), None)
    if pi and pi["ready"] >= 1: break
    time.sleep(0.5)
step("pool refilled after the hand-out", pi and pi["ready"] == 1, str(pi))
c, r = req("DELETE", f"/templates/pool/{tpl['key']}"); c, h = req("GET", "/health")
step("pool dropped, warm VM destroyed", c == 200 and h.get("vms_pooled") == 0, f"pooled={h.get('vms_pooled')}")
req("DELETE", f"/sandboxes/{pz['id']}")

print("== deleting the template does not break sandboxes started from it ==")
c, r = req("DELETE", f"/templates/{tpl['key']}"); step("template snapshot deleted", c == 200)
req("POST", f"/sandboxes/{a['id']}/pause"); r = ex(a["id"], "cat /home/agent/state | head -1")
step("sandbox still pauses/resumes (hardlinked layers)", r["stdout"].strip() == "v1", r["stdout"].strip())

if RESTART:
    print("== SIGTERM drain: running persistent -> paused, ephemeral -> gone ==")
    c, e = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 300, "mem": 256})
    ex(a["id"], "echo drained > /home/agent/marker")
    c, before = req("GET", f"/sandboxes/{a['id']}"); assert before["status"] == "running"
    t0 = time.time(); subprocess.run(RESTART, shell=True, check=True); trs = time.time() - t0
    for _ in range(60):
        try: c, lst = req("GET", "/sandboxes"); break
        except Exception: time.sleep(0.5)
    st = {x["id"]: x["status"] for x in lst["sandboxes"]}
    step("running persistent sandbox came back paused", st.get(a["id"]) == "paused", f"{st} restart took {trs:.1f}s")
    step("ephemeral sandbox destroyed on drain", e["id"] not in st)
    r = ex(a["id"], "cat /home/agent/marker; cat /home/agent/state | wc -l")
    step("in-memory + disk state survived the node restart", r["stdout"].split() == ["drained", "5"], r["stdout"].strip().replace("\n", " "))
    c, au = req("GET", "/audit?n=40"); print("   ", [e["op"] for e in au["events"] if e["op"].startswith("drain") or e["op"] in ("recover", "orphan_removed")])

print("== cleanup ==")
req("DELETE", f"/sandboxes/{a['id']}")

def wait_node():
    for _ in range(60):
        try: return req("GET", "/health")[1]
        except Exception: time.sleep(0.5)

if RESTART_NOJAIL and not h.get("uffd"):
    print("== uffd path: node restarted unjailed (the shared host has vm.unprivileged_userfaultfd=0) ==")
    subprocess.run(RESTART_NOJAIL, shell=True, check=True); h2 = wait_node()
    step("unjailed node reports uffd available", h2 and h2.get("uffd") and not h2.get("jail"), str(h2.get("uffd_note")))
    c, t2 = req("POST", "/templates/snapshot", {"mem": 512, "vcpu": 2})
    c, x = req("POST", "/sandboxes", {"class": "persistent", "ttl_idle_s": 300, "mem": 512, "boot": "snapshot"})
    ex(x["id"], "echo u1 > /home/agent/state; x=$(head -c 48M /dev/zero | tr '\\0' a); unset x")
    req("POST", f"/sandboxes/{x['id']}/pause")
    t0 = time.time(); r = ex(x["id"], "cat /home/agent/state; sha256sum /bin/bash | cut -c1-8; echo u2 >> /home/agent/state"); tr = time.time() - t0
    c, i = req("GET", f"/sandboxes/{x['id']}"); u = i.get("uffd") or {}
    step("resume over uffd keeps state, no merge", r["stdout"].split()[0] == "u1" and uffd_ok(i, u), f"{tr:.2f}s " + uffd_detail(i, u))
    step("handler served faults in blocks (far fewer faults than pages)", u.get("faults", 0) * 8 < u.get("pages_copied", 0) + u.get("pages_zero", 0), f"{u.get('faults')} faults for {u.get('pages_copied', 0) + u.get('pages_zero', 0)} pages")
    r = ex(x["id"], "x=$(head -c 200M /dev/zero | tr '\\0' a); unset x; sleep 3; cat /home/agent/state | wc -l")
    c, i = req("GET", f"/sandboxes/{x['id']}"); u2 = i.get("uffd") or {}
    step("balloon remove events handled (pages given back are tracked)", r["stdout"].strip() == "2" and not u2.get("error"), f"removes={u2.get('removes')} pages_removed={u2.get('pages_removed')} eagain={u2.get('eagain')}")
    req("POST", f"/sandboxes/{x['id']}/pause"); r = ex(x["id"], "cat /home/agent/state | tr '\\n' ' '")
    c, i = req("GET", f"/sandboxes/{x['id']}"); u3 = i.get("uffd") or {}
    step("second pause/resume cycle: Diff taken from a uffd-backed VM, resumed over uffd again (3 layers)", r["stdout"].split() == ["u1", "u2"] and u3.get("layers") == 3 and not u3.get("error"), f"layers={u3.get('layers')} " + uffd_detail(i, u3))
    c, f = req("POST", f"/sandboxes/{x['id']}/fork", {"n": 1, "ttl_idle_s": 60})
    step("fork from a uffd-backed VM", c == 201 and ex(f["clones"][0]["id"], "cat /home/agent/state | head -1")["stdout"].strip() == "u1")
    for sid in [x["id"]] + [cc["id"] for cc in f.get("clones", [])]: req("DELETE", f"/sandboxes/{sid}")
    req("DELETE", f"/templates/{t2['key']}")
    subprocess.run(RESTART, shell=True, check=True); h3 = wait_node()
    step("node back in jail mode", h3 and h3.get("jail"), str(h3.get("uffd_note")))
c, sb_ = req("GET", "/sandboxes"); left_ = [x["id"] for x in sb_.get("sandboxes", []) if x["id"] not in BEFORE]; step("all gone", c == 200 and not left_, f"{len(left_)} left {left_[:3]}")
