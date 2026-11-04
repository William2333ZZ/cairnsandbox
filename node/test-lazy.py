#!/usr/bin/env python3
"""Lazy image loading: cold/warm/prefetched boots vs an eager full pull, bytes fetched vs image size.
usage: test-lazy.py http://host:port <node-container> <store-url-as-seen-from-node> [restart-node-cmd]"""
import json, os, subprocess, sys, time, urllib.request

BASE, CTR, STORE = sys.argv[1], sys.argv[2], sys.argv[3]; RESTART = sys.argv[4] if len(sys.argv) > 4 else None
AUTH = {"Authorization": "Bearer " + __import__("os").environ["NODE_TOKEN"]} if __import__("os").environ.get("NODE_TOKEN") else {}
def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=900) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))   # printed even when the script crashes
def stats(): return req("GET", "/lazy/stats")[1]
def mib(b): return f"{b/1048576:.0f} MiB"
def wait_node():
    for _ in range(60):
        try: req("GET", "/health"); return
        except Exception: time.sleep(0.5)

def boot_once(label, **extra):
    s0 = stats(); t0 = time.time()
    c, a = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 120, **extra})
    t = time.time() - t0; s1 = stats()
    fetched = s1["bytes_fetched"] - s0["bytes_fetched"]; chunks = s1["chunks_fetched"] - s0["chunks_fetched"]
    size = s1["image_sizes"][BASE_IMG]
    print(f"   {label}: ready in {t:.2f}s, fetched {mib(fetched)} ({chunks} chunks, {100*fetched/size:.1f}% of base), hits {s1['chunks_hit']-s0['chunks_hit']}")
    r = req("POST", f"/sandboxes/{a['id']}/exec", {"cmd": "echo $((6*7)); ls /usr/bin | wc -l"})[1]
    return a["id"], t, fetched, r["stdout"].split()

if RESTART:   # make "cold" really cold: clear the cache and restart the node first
    subprocess.run(RESTART, shell=True, check=True); wait_node()
st = stats(); print("store images:", st["image_sizes"], "cache:", st["cached_chunks"], "chunks")
_h = req("GET", "/health")[1]; BASE_IMG = os.environ.get("BASE_IMAGE") or {"debian-13": "base-debian-13.ext4"}.get(_h.get("default_template"), "base.ext4")
print(f"   base image: {BASE_IMG} (default template {_h.get('default_template')})")
BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}   # the node is shared: only sandboxes that appear during this run count as left behind
print("== cold boot (empty cache) ==")
A, t_cold, f_cold, out = boot_once("cold")
step("guest works on a lazily loaded base", out[:1] == ["42"], str(out))
print("== warm boot (same node, cache populated) ==")
B, t_warm, f_warm, _ = boot_once("warm")
step("warm boot fetches ~nothing", f_warm < 2 << 20, mib(f_warm))
req("DELETE", f"/sandboxes/{A}"); req("DELETE", f"/sandboxes/{B}")

print("== boot profile ==")
c, prof = req("GET", f"/lazy/profile/{BASE_IMG}"); n_prof = len(prof["chunks"])
print(f"   chunks touched so far: {n_prof} = {mib(n_prof << 20)}")
subprocess.run(["docker", "exec", CTR, "bash", "-c", f"echo '{json.dumps(prof['chunks'])}' > /tmp/p.json"], check=True)
subprocess.run(f"docker cp {CTR}:/tmp/p.json /tmp/fc-profile.json && docker cp /tmp/fc-profile.json fc-store:/tmp/p.json && docker exec fc-store python3 /usr/local/bin/imgstore.py profile /store {BASE_IMG} /tmp/p.json", shell=True, check=True)

if RESTART:
    print("== prefetched boot (fresh node, cache cleared, profile prefetched first) ==")
    subprocess.run(RESTART, shell=True, check=True); wait_node()
    c, pf = req("POST", "/lazy/prefetch", {"image": BASE_IMG}); print(f"   prefetch {pf['chunks']} chunks in {pf['s']}s")
    C, t_pre, f_pre, _ = boot_once("prefetched"); req("DELETE", f"/sandboxes/{C}")
    step("prefetched boot fetches less during boot than cold", f_pre < f_cold, f"{mib(f_pre)} vs {mib(f_cold)}; {t_pre:.2f}s vs {t_cold:.2f}s")

    print("== eager baseline: download the whole base image first ==")
    out = subprocess.run(["docker", "exec", CTR, "python3", "/usr/local/bin/imgstore.py", "pull", STORE, BASE_IMG, "/tmp/eager.ext4"], capture_output=True, text=True).stdout
    e = json.loads(out.strip().splitlines()[-1]); print(f"   eager pull: {mib(e['bytes'])} in {e['s']}s (+ ~0.8s boot)")
    step("lazy cold boot beats eager pull + boot", t_cold < e["s"] + 0.8, f"{t_cold:.2f}s vs {e['s']+0.8:.2f}s")

    print("== 3 concurrent boots on a cold cache ==")
    subprocess.run(RESTART, shell=True, check=True); wait_node(); s0 = stats(); t0 = time.time()
    import threading
    ids = [None] * 3
    def mk(i): ids[i] = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 120})[1].get("id")
    th = [threading.Thread(target=mk, args=(i,)) for i in range(3)]; [t.start() for t in th]; [t.join() for t in th]
    wall = time.time() - t0; s1 = stats()
    print(f"   3 VMs ready in {wall:.2f}s wall, fetched {mib(s1['bytes_fetched']-s0['bytes_fetched'])} total (cache shared across VMs)")
    step("all 3 booted", all(ids), str(ids))
    for i in ids: req("DELETE", f"/sandboxes/{i}")

print("== pmem+DAX over the lazy file ==")
D_, t_p, f_p, out = boot_once("pmem over lazyfs", pmem_base=True)
step("pmem base works on the FUSE-backed file", out[:1] == ["42"], str(out)); req("DELETE", f"/sandboxes/{D_}")
c, sb_ = req("GET", "/sandboxes"); left_ = [x["id"] for x in sb_.get("sandboxes", []) if x["id"] not in BEFORE]; step("all gone", c == 200 and not left_, f"{len(left_)} left {left_[:3]}")
