#!/usr/bin/env python3
"""Latency of the E2B data plane WITHOUT an SDK and without a tunnel: stdlib only, speaks the same Connect JSON / REST an SDK does, so it can run on the
control plane's host (which has no network to install anything). Numbers are the stack's own cost: control plane -> node -> vsock -> sandboxd.

  python3 ops/e2b-latency-raw.py --url http://127.0.0.1:18710 --key <token> [--n 50] [--par 4] [--json out.json]
"""
import argparse, base64, http.client, json, os, struct, threading, time, urllib.parse


class C:
    def __init__(self, url, key):
        u = urllib.parse.urlparse(url); self.host, self.port, self.key = u.hostname, u.port or 80, key
        self.tls = threading.local()

    def conn(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=120)

    def api(self, method, path, body=None):
        c = self.conn(); c.request(method, path, json.dumps(body) if body is not None else None, {"X-API-Key": self.key, "Content-Type": "application/json"})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data else None)

    def envd(self, sb, method, path, body=b"", ctype="application/json", extra=None):
        c = self.conn(); h = {"E2b-Sandbox-Id": sb["sandboxID"], "E2b-Sandbox-Port": "49983", "X-Access-Token": sb["envdAccessToken"], "Content-Type": ctype, **(extra or {})}
        c.request(method, path, body, h); r = c.getresponse(); return c, r

    def run(self, sb, cmd, first_byte=None):
        msg = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd]}, "stdin": False}).encode()
        c, r = self.envd(sb, "POST", "/process.Process/Start", b"\x00" + struct.pack(">I", len(msg)) + msg, "application/connect+json")
        out, t0, code = b"", time.perf_counter(), None
        while True:
            hdr = r.read(5)
            if len(hdr) < 5: break
            n = struct.unpack(">I", hdr[1:])[0]; data = r.read(n) if n else b""
            if hdr[0] & 2: break
            ev = json.loads(data).get("event", {})
            if "data" in ev:
                if first_byte is not None and not first_byte: first_byte.append(time.perf_counter() - t0)
                out += base64.b64decode(ev["data"].get("stdout", ""))
            if "end" in ev: code = ev["end"].get("exitCode", 0)
        c.close(); assert code == 0, (code, out); return out

    def write(self, sb, path, data):
        c, r = self.envd(sb, "POST", "/files?path=" + urllib.parse.quote(path), data, "application/octet-stream"); r.read(); c.close(); assert r.status == 200, r.status

    def read(self, sb, path):
        c, r = self.envd(sb, "GET", "/files?path=" + urllib.parse.quote(path), b""); d = r.read(); c.close(); assert r.status == 200, r.status; return d


def pct(xs, p): xs = sorted(xs); return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]
def stat(xs): return {"n": len(xs), "p50_ms": round(pct(xs, 50) * 1000, 1), "p95_ms": round(pct(xs, 95) * 1000, 1), "max_ms": round(max(xs) * 1000, 1)}
def timed(f, n):
    o = []
    for _ in range(n): t = time.perf_counter(); f(); o.append(time.perf_counter() - t)
    return o


ap = argparse.ArgumentParser(); ap.add_argument("--url", required=True); ap.add_argument("--key", required=True); ap.add_argument("--n", type=int, default=50); ap.add_argument("--par", type=int, default=4); ap.add_argument("--json")
a = ap.parse_args(); c = C(a.url, a.key); res = {}
def create():
    st, sb = c.api("POST", "/v2/sandboxes", {"templateID": "base", "timeout": 600}); assert st == 201, (st, sb); return sb
res["http GET /health (control plane only)"] = stat(timed(lambda: c.api("GET", "/health"), a.n))
t = time.perf_counter(); sb = create(); res["create (cold boot)"] = {"seconds": round(time.perf_counter() - t, 3)}
try:
    c.run(sb, "true")
    res["Start: true -> end"] = stat(timed(lambda: c.run(sb, "true"), a.n))
    res["files write 1 KiB"] = stat(timed(lambda: c.write(sb, "/tmp/l.txt", b"x" * 1024), a.n))
    res["files read 1 KiB"] = stat(timed(lambda: c.read(sb, "/tmp/l.txt"), a.n))
    blob = os.urandom(64 << 20); t = time.perf_counter(); c.write(sb, "/tmp/l.bin", blob); w = time.perf_counter() - t
    t = time.perf_counter(); back = c.read(sb, "/tmp/l.bin"); r = time.perf_counter() - t; assert back == blob
    res["files 64 MiB"] = {"write_MBps": round(64 / w, 1), "read_MBps": round(64 / r, 1)}
    fb = []; c.run(sb, "echo go; sleep 1", fb); res["first stdout byte after Start sent (ms)"] = round(fb[0] * 1000, 1) - 0.0
finally:
    c.api("DELETE", "/sandboxes/" + sb["sandboxID"])
sbs, lat, errs = [], [], []
def worker(s):
    try:
        for _ in range(a.n // 2): t = time.perf_counter(); c.run(s, "echo $((6*7))"); lat.append(time.perf_counter() - t)
    except Exception as e: errs.append(repr(e)[:120])  # noqa: BLE001
try:
    t0 = time.perf_counter(); sbs = [create() for _ in range(a.par)]; mk = time.perf_counter() - t0
    ths = [threading.Thread(target=worker, args=(s,)) for s in sbs]; t0 = time.perf_counter(); [t.start() for t in ths]; [t.join() for t in ths]; wall = time.perf_counter() - t0
    res["parallel"] = {"sandboxes": a.par, "create_all_s": round(mk, 2), "commands": len(lat), "errors": errs, "wall_s": round(wall, 2), "commands_per_s": round(len(lat) / wall, 1), **(stat(lat) if lat else {})}
finally:
    for s in sbs: c.api("DELETE", "/sandboxes/" + s["sandboxID"])
print(json.dumps(res, indent=1))
if a.json: json.dump(res, open(a.json, "w"), indent=1)
