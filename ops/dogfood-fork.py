#!/usr/bin/env python3
"""Dogfood experiment 2 (docs/research/dogfood-dev-loop-2026-11.md §12): does a checkpoint + fork beat redoing the setup?

    CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=<token> python3 ops/dogfood-fork.py <src.tar> [template-name] [shards]

The real workload is our own Go suite. One sandbox from the `dev-go` template unpacks the source and takes a checkpoint ("ready": Postgres up, build cache warm).
  A. serial: the whole suite in that sandbox, then rollback to "ready" (how long does going back take, is the state really back?)
  B. parallel: fork N copies from "ready", one package shard each, all at once; the wall time is fork + the slowest shard.
  C. isolation: a file written in one clone is not in the others nor in the parent; Postgres in each clone is its own.
Prints one JSON line of timings; leaves nothing behind. The token is read from the environment and never printed."""
import json, os, sys, threading, time, urllib.error, urllib.request

URL = os.environ.get("CAIRN_URL", "http://127.0.0.1:18700").rstrip("/"); TOKEN = os.environ["CAIRN_TOKEN"]
PROJECT = os.environ.get("CAIRN_PROJECT", "dogfood")
PG = "postgres://postgres@127.0.0.1:5432/postgres?sslmode=disable"
GO = f". /etc/profile.d/go.sh; export CAIRN_TEST_PG='{PG}'; cd /work/cp && "


def call(method, path, body=None, raw=None, timeout=900):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Authorization": "Bearer " + TOKEN, "X-Project": PROJECT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(); return r.status, (json.loads(b) if b[:1] in (b"{", b"[") else b)
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:300].decode(errors="replace")})


def ex(sb, cmd, user="root", timeout=900):
    st, r = call("POST", f"/sandboxes/{sb}/exec", {"cmd": cmd, "user": user, "timeout": timeout}, timeout=timeout + 30)
    if st != 200: raise SystemExit(f"exec failed {st}: {r}")
    return r


def shards(parent, n):
    pk = [p for p in ex(parent, GO + "go list ./...")["stdout"].split() if p]
    heavy = next(p for p in pk if p.endswith("/httpapi"))
    rest = [p for p in pk if p != heavy]
    out = [[heavy]] + [[] for _ in range(n - 1)]
    for i, p in enumerate(rest): out[1 + i % (n - 1)].append(p)
    return out


def main(src, name, n):
    t = {}; made = []
    st, tl = call("GET", "/templates"); tpl = next((x for x in tl["templates"] if x.get("key") == name), None)
    if not tpl: raise SystemExit(f"no template {name}")
    try:
        t0 = time.monotonic()
        st, p = call("POST", "/sandboxes", {"class": "persistent", "template_key": name, "network": {"mode": "allowlist", "allow_hosts": []}}); parent = p["id"]; made.append(parent)
        call("PUT", f"/sandboxes/{parent}/files?path=/tmp/src.tar", raw=open(src, "rb").read(), timeout=300)
        ex(parent, "mkdir -p /work && tar -xf /tmp/src.tar -C /work")
        if os.environ.get("COLD"):   # an expensive mid-task state: the template's warm build cache thrown away, then rebuilt (that rebuild is what the checkpoint saves each clone)
            ex(parent, "rm -rf /var/cache/gocache/*"); t0c = time.monotonic()
        ex(parent, GO + "go test -count=1 -run '^$' ./... >/dev/null 2>&1; echo warm")
        if os.environ.get("COLD"): t["rebuild_s"] = round(time.monotonic() - t0c, 2)
        t["prepare_s"] = round(time.monotonic() - t0, 2)
        t0 = time.monotonic(); st, c = call("POST", f"/sandboxes/{parent}/checkpoints", {"name": "ready", "note": "source unpacked, cache warm, postgres up"})
        t["checkpoint_s"] = round(time.monotonic() - t0, 2); assert st == 201, c
        plan = shards(parent, n)

        # A. serial in the parent, then roll back
        ex(parent, "echo dirty > /tmp/dirty-marker")
        t0 = time.monotonic(); r = ex(parent, GO + "go test -count=1 ./... 2>&1 | tail -3", timeout=1500); t["serial_s"] = round(time.monotonic() - t0, 2); t["serial_exit"] = r["exit"]
        t0 = time.monotonic(); st, rb = call("POST", f"/sandboxes/{parent}/rollback", {"checkpoint": "ready"}); t["rollback_s"] = round(time.monotonic() - t0, 2)
        back = ex(parent, "test -e /tmp/dirty-marker && echo still-dirty || echo clean; pg_isready >/dev/null && echo pg-up")["stdout"].split()
        t["rollback_restored"] = back == ["clean", "pg-up"]

        # B. fork and run one shard per clone
        t0 = time.monotonic(); st, f = call("POST", f"/sandboxes/{parent}/fork", {"n": n, "checkpoint": "ready"})
        if st != 201: raise SystemExit(f"fork failed {st}: {f}")
        clones = [x["id"] for x in f["clones"]]; made += clones; t["fork_s"] = round(time.monotonic() - t0, 2)
        t["clones_ready"] = all("accepting connections" in ex(c, "pg_isready")["stdout"] for c in clones)
        res = [None] * n
        def work(i):
            t1 = time.monotonic(); r = ex(clones[i], GO + "go test -count=1 " + " ".join(plan[i]) + " 2>&1 | tail -4", timeout=1500)
            res[i] = {"s": round(time.monotonic() - t1, 2), "exit": r["exit"], "pkgs": len(plan[i])}
        t0 = time.monotonic(); th = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        [x.start() for x in th]; [x.join() for x in th]
        t["parallel_s"] = round(time.monotonic() - t0, 2); t["shards"] = res; t["parallel_total_s"] = round(t["fork_s"] + t["parallel_s"], 2)

        # C. isolation
        ex(clones[0], "echo only-here > /tmp/iso-marker")
        seen = [ex(x, "test -e /tmp/iso-marker && echo yes || echo no")["stdout"].strip() for x in [parent] + clones]
        t["isolated"] = seen == ["no", "yes"] + ["no"] * (n - 1)
        print(json.dumps(t))
    finally:
        for sid in made: call("DELETE", f"/sandboxes/{sid}")


if __name__ == "__main__":
    if len(sys.argv) < 2: print(__doc__); sys.exit(2)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "dev-go", int(sys.argv[3]) if len(sys.argv) > 3 else 3)
