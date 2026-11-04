#!/usr/bin/env python3
"""Dogfood experiment 3 (docs/research/dogfood-dev-loop-2026-11.md §13): a real task through the egress approval flow, then the audit chain.

    CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=<admin token> python3 ops/dogfood-egress.py <src.tar> [template-name]

The task is ours: run govulncheck on the control plane (D73 left it manual because it needs the Go proxy and vuln.go.dev). A project *member* (not the deployer) runs it in a
sandbox that starts with the egress closed and has to ask for what it needs; the deployer approves. Nothing is planned in advance: the member asks for the host the failure
names, so the count of rounds is what a real agent would hit. Afterwards the audit log is read back to see whether it tells the story, and the hash chain is verified.
The member is created for the run and deleted at the end; tokens are never printed."""
import json, os, re, sys, time, urllib.error, urllib.request

URL = os.environ.get("CAIRN_URL", "http://127.0.0.1:18700").rstrip("/"); ADMIN = os.environ["CAIRN_TOKEN"]
PROJECT = os.environ.get("CAIRN_PROJECT", "dogfood"); BOT = "dogfood-bot"
VULN = os.environ.get("GOVULNCHECK", "v1.1.4")
ENV = ". /etc/profile.d/go.sh; export GOPROXY=" + os.environ.get("GOPROXY_URL", "https://proxy.golang.org") + " GOSUMDB=off GOBIN=/tmp/bin GOMODCACHE=/tmp/gomod2 GOCACHE=/tmp/gocache2 GOTOOLCHAIN=local; "


def call(method, path, body=None, raw=None, token=None, timeout=900):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Authorization": "Bearer " + (token or ADMIN), "X-Project": PROJECT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(); return r.status, (json.loads(b) if b[:1] in (b"{", b"[") else b)
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:300].decode(errors="replace")})


def main(src, name):
    log = []; made = None; bot = None
    def note(step, **kw): log.append({"step": step, **kw}); print(json.dumps(log[-1]), flush=True)
    st, m = call("POST", f"/projects/{PROJECT}/members", {"user": BOT, "role": "member"})
    if st != 201: raise SystemExit(f"member: {st} {m}")
    bot = m["token"]
    try:
        mem = int(os.environ.get("MEM", "0"))   # govulncheck on the control plane peaks near 2 GiB: with MEM set, boot the template's layers cold with that much memory (template_key + boot:cold + mem)
        st, tl = call("GET", "/templates"); tpl = next((x for x in tl["templates"] if x.get("key") == name), {})
        body = {"class": "persistent", "template_key": name, **({"mem": mem, "boot": "cold"} if mem else {})}      # D80: the template's layers at the size asked for, booted fresh
        t0 = time.monotonic(); st, s = call("POST", "/sandboxes", body, token=bot)   # no network given: the project default (closed)
        if st != 201: raise SystemExit(f"create as member {st}: {s}")
        sb = made = s["id"]; note("member creates sandbox", s=round(time.monotonic() - t0, 2), network=(s.get("network") or {}).get("mode"))
        call("PUT", f"/sandboxes/{sb}/files?path=/tmp/src.tar", raw=open(src, "rb").read(), token=bot)
        def ex(cmd, timeout=600, user="agent"):
            st, r = call("POST", f"/sandboxes/{sb}/exec", {"cmd": cmd, "user": user, "timeout": timeout}, token=bot, timeout=timeout + 30); return r
        ex("mkdir -p /work && tar -xf /tmp/src.tar -C /work", user="root")
        rounds = 0; approved_hosts = []
        install = ENV + f"go install golang.org/x/vuln/cmd/govulncheck@{VULN} 2>&1 | tail -4; test -x /tmp/bin/govulncheck && echo INSTALLED"
        while True:
            r = ex(install, 300)
            if "INSTALLED" in r["stdout"]: note("govulncheck installed", rounds=rounds, hosts=approved_hosts); break
            hosts = [h for h in re.findall(r"(?:https?://|dial tcp )([a-z0-9.-]+\.[a-z]{2,})", r["stdout"]) if h not in approved_hosts]
            note("install failed", tail=r["stdout"][-300:].strip(), asks=hosts[:1])
            if not hosts or rounds >= 4: raise SystemExit("cannot make progress")
            rounds += 1; ask(sb, bot, hosts[0], approved_hosts, note)
        run = ENV + "export GOPROXY=off GOMODCACHE=/opt/gomod; cd /work/cp && /tmp/bin/govulncheck ./... 2>&1 | tail -25; echo EXIT=${PIPESTATUS[0]}"
        for _ in range(4):
            t1 = time.monotonic(); r = ex(run, 600)
            out = r["stdout"]
            hosts = [h for h in re.findall(r"(vuln\.go\.dev|[a-z0-9.-]+\.[a-z]{2,})(?::\d+)?", out) if h.endswith(("go.dev", "golang.org")) and h not in approved_hosts]
            if "EXIT=" in out and ("Fetching vulnerabilities" in out or "No vulnerabilities" in out or "Your code" in out or "Vulnerability #" in out or "found" in out) or not hosts:
                note("govulncheck ran", s=round(time.monotonic() - t1, 2), result=out[-700:].strip()); break
            note("govulncheck blocked", tail=out[-300:].strip(), asks=hosts[:1]); ask(sb, bot, hosts[0], approved_hosts, note)
        # tightening is immediate, loosening is not
        t2 = time.monotonic(); st, r = call("PUT", f"/sandboxes/{sb}/network", {"mode": "closed", "allow_hosts": []}, token=bot); note("member tightens to closed", status=st, s=round(time.monotonic() - t2, 2))
        st, eg = call("GET", f"/sandboxes/{sb}/egress", token=bot); ev = eg.get("decisions") or []
        note("egress log", entries=len(ev), hosts=sorted({e.get("host") for e in ev if e.get("host")}), allowed=sum(1 for e in ev if e.get("allowed")), denied=sum(1 for e in ev if not e.get("allowed")))
        # the audit story
        st, au = call("GET", "/audit?n=400"); evs = [e for e in au.get("events", []) if e.get("sandbox") == sb or e.get("by") == BOT]    # the relax events carry no project, so no project filter
        ops = [e.get("op") for e in reversed(evs)]
        note("audit for this run", events=len(evs), ops=ops, exec_audited=any(str(e.get("op", "")).startswith("exec") for e in evs))
        st, vf = call("GET", "/audit/verify"); note("audit chain", ok=vf.get("ok"), lines=vf.get("lines"), first_bad=vf.get("first_bad"))
    finally:
        if made: call("DELETE", f"/sandboxes/{made}")
        call("DELETE", f"/projects/{PROJECT}/members/{BOT}")


def ask(sb, bot, host, approved, note):
    t = time.monotonic(); st, r = call("PUT", f"/sandboxes/{sb}/network", {"mode": "allowlist", "allow_hosts": approved + [host]}, token=bot)
    if st != 202: note("relax request", host=host, status=st, body=str(r)[:200]); return
    rid = r["request"]["id"]; t_ask = time.monotonic() - t
    t = time.monotonic(); st2, a = call("POST", f"/network/requests/{rid}/approve"); approved.append(host)
    note("relax needs approval", host=host, ask_status=st, ask_s=round(t_ask, 2), approve_status=st2, approve_s=round(time.monotonic() - t, 2))


if __name__ == "__main__":
    if len(sys.argv) < 2: print(__doc__); sys.exit(2)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "dev-go")
