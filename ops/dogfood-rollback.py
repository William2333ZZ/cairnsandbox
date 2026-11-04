#!/usr/bin/env python3
"""Dogfood gap 6 (docs/research/dogfood-dev-loop-2026-11.md §14): what do checkpoint / rollback / fork latency scale with, memory or disk?

    CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=<token> python3 ops/dogfood-rollback.py [template-name] [GiB]

One small sandbox, three states, each checkpointed then rolled back and forked: idle; GiB of anonymous memory held (no disk written); GiB written to a file (little memory kept).
Prints one JSON line per state with the node's per-phase restore timings. Light by design (one 2 GiB sandbox, clones deleted at once). The token is read from the environment, never printed."""
import json, os, sys, time, urllib.error, urllib.request

URL = os.environ.get("CAIRN_URL", "http://127.0.0.1:18700").rstrip("/"); TOKEN = os.environ["CAIRN_TOKEN"]
PROJECT = os.environ.get("CAIRN_PROJECT", "dogfood")


def call(method, path, body=None, timeout=900):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + TOKEN, "X-Project": PROJECT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(); return r.status, (json.loads(b) if b[:1] in (b"{", b"[") else b)
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:300].decode(errors="replace")})


def ex(sb, cmd, timeout=300):
    st, r = call("POST", f"/sandboxes/{sb}/exec", {"cmd": cmd, "user": "root", "timeout": timeout}, timeout=timeout + 30)
    if st != 200: raise SystemExit(f"exec failed {st}: {r}")
    return r


def measure(sb, tag):
    out = {"state": tag}
    r = ex(sb, "free -m | awk '/Mem:/{print $3}'; df -m / | awk 'NR==2{print $3}'")["stdout"].split()
    out["guest_used_mib"], out["guest_disk_used_mib"] = int(r[0]), int(r[1])
    ex(sb, "sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; sleep 3")   # the same settle the templates get, so the page cache is not what is measured
    name = "c-" + tag
    t = time.monotonic(); st, c = call("POST", f"/sandboxes/{sb}/checkpoints", {"name": name}); out["checkpoint_s"] = round(time.monotonic() - t, 3)
    t = time.monotonic(); st, rb = call("POST", f"/sandboxes/{sb}/rollback", {"checkpoint": name}); out["rollback_s"] = round(time.monotonic() - t, 3)
    out["rollback_phases"] = (rb or {}).get("phases") if isinstance(rb, dict) else None
    t = time.monotonic(); st, rb = call("POST", f"/sandboxes/{sb}/rollback", {"checkpoint": name}); out["rollback_again_s"] = round(time.monotonic() - t, 3)   # the merged memory file is cached by now
    out["rollback_again_phases"] = (rb or {}).get("phases") if isinstance(rb, dict) else None
    t = time.monotonic(); st, f = call("POST", f"/sandboxes/{sb}/fork", {"n": 1, "checkpoint": name}); out["fork1_s"] = round(time.monotonic() - t, 3)
    clones = [x["id"] for x in f.get("clones", [])] if isinstance(f, dict) else []
    for cid in clones: call("DELETE", f"/sandboxes/{cid}")
    t = time.monotonic(); st, f = call("POST", f"/sandboxes/{sb}/fork", {"n": 3, "checkpoint": name}); out["fork3_s"] = round(time.monotonic() - t, 3)
    clones = [x["id"] for x in f.get("clones", [])] if isinstance(f, dict) else []
    for cid in clones: call("DELETE", f"/sandboxes/{cid}")
    call("DELETE", f"/sandboxes/{sb}/checkpoints/{name}")
    print(json.dumps(out), flush=True)


def main(name, gib):
    st, tl = call("GET", "/templates"); tpl = next((x for x in tl["templates"] if x.get("key") == name), None)
    if not tpl: raise SystemExit(f"no template {name}")
    st, s = call("POST", "/sandboxes", {"class": "persistent", "template_key": name, "network": {"mode": "closed"}})
    if st != 201: raise SystemExit(f"create {st}: {s}")
    sb = s["id"]
    try:
        measure(sb, "idle")
        # anonymous memory held by a background process, nothing written to disk
        ex(sb, f"nohup python3 -c \"import time; b = bytearray({gib} << 30); b[::4096] = b'x' * len(b[::4096]); time.sleep(3600)\" >/dev/null 2>&1 & sleep 4")
        measure(sb, f"mem{gib}g")
        ex(sb, "pkill -f 'bytearray' || true; sleep 1")
        ex(sb, f"dd if=/dev/urandom of=/var/tmp/fill.bin bs=1M count={gib << 10} status=none; sync")     # incompressible, so it really is allocated in the writable layer
        measure(sb, f"disk{gib}g")
    finally:
        call("DELETE", f"/sandboxes/{sb}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dev-go-v2", int(sys.argv[2]) if len(sys.argv) > 2 else 1)
