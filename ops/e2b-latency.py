#!/usr/bin/env python3
"""Light latency and concurrency numbers for the E2B data plane, measured from where the SDK runs (so they include that network path: run it on the
control plane's host for the cost of the stack alone). Small on purpose: one sandbox for latency, up to --par sandboxes for the parallel part.

  E2B_API_URL=... E2B_SANDBOX_URL=... E2B_API_KEY=... python3 ops/e2b-latency.py [--n 30] [--par 4] [--json out.json]
"""
import argparse, json, os, statistics, threading, time, urllib.request
from e2b import Sandbox


def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def stat(xs):
    return {"n": len(xs), "p50_ms": round(pct(xs, 50) * 1000, 1), "p95_ms": round(pct(xs, 95) * 1000, 1), "max_ms": round(max(xs) * 1000, 1)}


def timed(f, n):
    out = []
    for _ in range(n):
        t = time.perf_counter(); f(); out.append(time.perf_counter() - t)
    return out


ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=30); ap.add_argument("--par", type=int, default=4); ap.add_argument("--json")
a = ap.parse_args()
res = {}
base = os.environ["E2B_API_URL"].rstrip("/")
res["http_rtt_to_e2b_port"] = stat(timed(lambda: urllib.request.urlopen(base + "/health", timeout=10).read(), a.n))    # the network path alone

t = time.perf_counter(); sb = Sandbox.create(timeout=600); res["create"] = {"seconds": round(time.perf_counter() - t, 3)}
try:
    sb.commands.run("true")                                          # warm the connection
    res["commands.run('true')"] = stat(timed(lambda: sb.commands.run("true"), a.n))
    res["files.write 1 KiB"] = stat(timed(lambda: sb.files.write("/tmp/lat.txt", "x" * 1024), a.n))
    res["files.read 1 KiB"] = stat(timed(lambda: sb.files.read("/tmp/lat.txt"), a.n))
    blob = os.urandom(32 << 20); t = time.perf_counter(); sb.files.write("/tmp/lat.bin", blob); w = time.perf_counter() - t
    t = time.perf_counter(); back = sb.files.read("/tmp/lat.bin", format="bytes"); r = time.perf_counter() - t
    assert back == blob
    res["files 32 MiB"] = {"write_MBps": round(32 / w, 1), "read_MBps": round(32 / r, 1)}
    t = time.perf_counter(); first = []
    sb.commands.run("echo go; sleep 1", on_stdout=lambda d: first.append(time.perf_counter() - t)); res["first stdout byte after command start (ms)"] = round(first[0] * 1000, 1)
finally:
    sb.kill()

# parallel: several sandboxes, each looping commands
sbs, errs, lat = [], [], []
def worker(s):
    try:
        for _ in range(10):
            t = time.perf_counter(); s.commands.run("echo $((6*7))"); lat.append(time.perf_counter() - t)
    except Exception as e:  # noqa: BLE001
        errs.append(repr(e)[:120])
try:
    t0 = time.perf_counter(); sbs = [Sandbox.create(timeout=600) for _ in range(a.par)]; mk = time.perf_counter() - t0
    ths = [threading.Thread(target=worker, args=(s,)) for s in sbs]; t0 = time.perf_counter()
    [t.start() for t in ths]; [t.join() for t in ths]; wall = time.perf_counter() - t0
    res["parallel"] = {"sandboxes": a.par, "create_all_s": round(mk, 2), "commands": len(lat), "errors": errs, "wall_s": round(wall, 2), "commands_per_s": round(len(lat) / wall, 1), **(stat(lat) if lat else {})}
finally:
    for s in sbs:
        try: s.kill()
        except Exception: pass
print(json.dumps(res, indent=1))
if a.json: json.dump(res, open(a.json, "w"), indent=1)
