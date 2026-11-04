#!/usr/bin/env python3
"""Dogfood experiment 1 (docs/research/dogfood-dev-loop-2026-11.md): run our own Go tests inside a sandbox instead of on the shared box.

    CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=<token> python3 ops/dogfood-devloop.py prepare <dev-layer> <gotool-layer> [template-name]
    CAIRN_URL=... CAIRN_TOKEN=... python3 ops/dogfood-devloop.py run <src.tar> [template-name] [go test args...]

`prepare` boots a sandbox on the two layers, starts PostgreSQL, commits it as a template (memory included, so Postgres is already running
in every sandbox born from it). `run` births a sandbox from that template, unpacks the source, runs the tests with CAIRN_TEST_PG and prints timings.
The token is read from the environment and never printed."""
import json, os, sys, time, urllib.error, urllib.request

URL = os.environ.get("CAIRN_URL", "http://127.0.0.1:18700").rstrip("/"); TOKEN = os.environ["CAIRN_TOKEN"]
PROJECT = os.environ.get("CAIRN_PROJECT", "dogfood")
PG = "postgres://postgres@127.0.0.1:5432/postgres?sslmode=disable"


def call(method, path, body=None, raw=None, timeout=900):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Authorization": "Bearer " + TOKEN, "X-Project": PROJECT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(); return r.status, (json.loads(b) if b[:1] in (b"{", b"[") else b)
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:300].decode(errors="replace")})


def ex(sb, cmd, user="root", timeout=900, cwd=None):
    st, r = call("POST", f"/sandboxes/{sb}/exec", {"cmd": cmd, "user": user, "timeout": timeout, **({"cwd": cwd} if cwd else {})}, timeout=timeout + 30)
    if st != 200: raise SystemExit(f"exec failed {st}: {r}")
    return r


def create(**kw):
    base = os.environ.get("BASE_TEMPLATE")      # e.g. debian-13-v2: prepare on another base image than the node default
    st, r = call("POST", "/sandboxes", {"class": "persistent", "vcpu": 2, "mem": 2048, "disk": 4096, **({"template": base} if base else {}), **kw})
    if st not in (200, 201): raise SystemExit(f"create failed {st}: {r}")
    return r["id"]


def prepare(dev, gotool, name, src=None):
    sb = create(layers=[dev, gotool], network={"mode": "allowlist", "allow_hosts": []})
    try:
        print("sandbox", sb)
        script = r"""set -e
ls /etc/postgresql 2>&1 || true
pg_lsclusters || true
V=$(ls /usr/lib/postgresql | head -1)
[ -d /etc/postgresql/$V/main ] || pg_createcluster $V main
sed -i 's/^\(local\|host\)\(\s\+all\s\+all\s\+.*\)\(peer\|scram-sha-256\|md5\)$/\1\2trust/' /etc/postgresql/$V/main/pg_hba.conf
grep -q localhost /etc/hosts || echo '127.0.0.1 localhost' >> /etc/hosts
cat /etc/hosts | head -3
echo "listen_addresses = '127.0.0.1'" > /etc/postgresql/$V/main/conf.d/aios.conf
pg_ctlcluster $V main start
for i in $(seq 1 30); do pg_isready -q && break; sleep 1; done
pg_isready
psql -U postgres -h 127.0.0.1 -Atc 'select version()'
"""
        r = ex(sb, script); print(r["stdout"][-600:], r["stderr"][-400:])
        if r["exit"] != 0: raise SystemExit("postgres setup failed")
        if src:  # warm the Go build cache so the first test run in every sandbox born from the template only compiles what changed
            call("PUT", f"/sandboxes/{sb}/files?path=/tmp/src.tar", raw=open(src, "rb").read(), timeout=300)
            r = ex(sb, "mkdir -p /work /var/cache/gocache && tar -xf /tmp/src.tar -C /work && echo 'export GOCACHE=/var/cache/gocache' >> /etc/profile.d/go.sh && . /etc/profile.d/go.sh && cd /work/cp && go test -count=1 -run '^$' ./... 2>&1 | tail -3; chmod -R a+rwX /var/cache/gocache; du -sh /var/cache/gocache", timeout=1200)
            print("warm:", r["stdout"][-300:], r["stderr"][-200:])
        ex(sb, "sync; echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory; sleep %d" % int(os.environ.get("SETTLE_S", "10")), timeout=120)  # page cache from unpacking/compiling would bloat the memory snapshot (and every birth)
        st, b = call("POST", "/builds", {"kind": "commit", "sandbox": sb, "name": name, "note": "go 1.26 + postgres running"})
        print("commit", st, b if st >= 300 else b["id"])
        if st >= 300: raise SystemExit(1)
        while True:
            st, bb = call("GET", "/builds/" + b["id"])
            if bb.get("status") != "running": break
            time.sleep(2)
        print("build", bb.get("status"), bb.get("error") or bb.get("result"))
    finally:
        call("DELETE", "/sandboxes/" + sb)


def run(src, name, args):
    t0 = time.monotonic()
    st, tl = call("GET", "/templates"); tpl = next((t for t in tl.get("templates", []) if t.get("key") == name), None)
    if not tpl: raise SystemExit(f"no template {name}; run prepare first")
    sb = create(template_key=name, layers=tpl.get("layers") or [], vcpu=tpl["vcpu"], mem=tpl["mem"], disk=tpl.get("disk", 4096), network={"mode": "allowlist", "allow_hosts": []})
    t_born = time.monotonic() - t0
    try:
        ready = ex(sb, "pg_isready; echo $?", timeout=30)
        t_ready = time.monotonic() - t0
        st, _ = call("PUT", f"/sandboxes/{sb}/files?path=/tmp/src.tar", raw=open(src, "rb").read(), timeout=300)
        t_up = time.monotonic() - t0
        ex(sb, "mkdir -p /work && tar -xf /tmp/src.tar -C /work", timeout=120)
        t_unpack = time.monotonic() - t0
        env = f". /etc/profile.d/go.sh; export CAIRN_TEST_PG='{PG}'; cd /work/cp && "
        t1 = time.monotonic()
        r = ex(sb, env + "go test " + " ".join(args or ["./..."]) + " 2>&1 | tail -40", timeout=1500)
        t_test = time.monotonic() - t1
        pgx = ex(sb, "psql -U postgres -h 127.0.0.1 -Atc \"select sum(xact_commit) from pg_stat_database\"", timeout=30)["stdout"].strip()
        st, m = call("GET", f"/sandboxes/{sb}/metrics")
        print(r["stdout"][-3000:])
        print(json.dumps({"born_s": round(t_born, 2), "pg_ready": ready["stdout"].strip().replace("\n", " "), "ready_s": round(t_ready, 2), "upload_s": round(t_up - t_ready, 2),
                          "unpack_s": round(t_unpack - t_up, 2), "tests_s": round(t_test, 2), "total_s": round(time.monotonic() - t0, 2), "tests_exit": r["exit"], "pg_commits": pgx}))
        st, info = call("GET", "/sandboxes/" + sb)
        print("cpu_usec", info.get("cpu_usec") if isinstance(info, dict) else None)
        if os.environ.get("KEEP"): print("kept", sb); return
    except BaseException:
        raise
    call("DELETE", "/sandboxes/" + sb)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare" and len(sys.argv) >= 4: prepare(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "dev-go", sys.argv[5] if len(sys.argv) > 5 else None)
    elif cmd == "run" and len(sys.argv) >= 3: run(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("-") and not sys.argv[3].startswith(".") else "dev-go", [a for a in sys.argv[3:] if a.startswith(("-", "."))])
    else: print(__doc__); sys.exit(2)
