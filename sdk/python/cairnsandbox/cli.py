"""`aios`: the sandbox cloud from a shell (D63). Zero dependencies; the same environment as the SDK (CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT), or --url / --token / --project.

    cairn whoami
    cairn ls                                  cairn create --mem 512 --allow pypi.org     cairn exec sb-1a2b -- python3 -V
    cairn pause|resume|rm sb-1a2b             cairn cp ./a.txt sb-1a2b:/home/agent/a.txt  cairn cp sb-1a2b:/home/agent/out.txt ./
    cairn checkpoint sb-1a2b before           cairn rollback sb-1a2b before
    cairn build py-data --from python:3.12-slim --run 'pip install pandas' --allow pypi.org --allow files.pythonhosted.org
    cairn keys ls | create ci --role viewer --days 30 | revoke key-…
    cairn usage                               cairn status                                 (the public status needs no token)
"""
import argparse, json, os, sys, urllib.request

from .client import ApiError, Client


def _table(rows, cols, out):
    if not rows: print("(none)", file=out); return
    w = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    print("  ".join(c.upper().ljust(n) for c, n in zip(cols, w)), file=out)
    for r in rows: print("  ".join(str(r.get(c, "") if r.get(c) is not None else "-").ljust(n) for c, n in zip(cols, w)), file=out)


def _client(a):
    return Client(a.url, token=a.token, project=a.project)


def _split_remote(s):
    """'sb-1:/path' -> ('sb-1', '/path'); a plain path -> (None, path). A Windows drive letter is not a sandbox id (ids start with sb-)."""
    if s.startswith("sb-") and ":" in s: sid, _, p = s.partition(":"); return sid, p
    return None, s


def run(argv, out=None):
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="cairn", description="CairnSandbox cloud CLI")
    p.add_argument("--url"); p.add_argument("--token"); p.add_argument("--project"); p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami"); sub.add_parser("status"); sub.add_parser("ls"); sub.add_parser("usage")
    c = sub.add_parser("create"); c.add_argument("--template"); c.add_argument("--mem", type=int); c.add_argument("--vcpu", type=int); c.add_argument("--class", dest="cls", choices=["persistent", "ephemeral"])
    c.add_argument("--allow", action="append", default=[], help="allow outbound to this host (repeatable)")
    e = sub.add_parser("exec"); e.add_argument("sandbox"); e.add_argument("--timeout", type=int); e.add_argument("command", nargs=argparse.REMAINDER)
    for n in ("pause", "resume", "rm"): sub.add_parser(n).add_argument("sandbox")
    cp = sub.add_parser("cp"); cp.add_argument("src"); cp.add_argument("dst")
    ck = sub.add_parser("checkpoint"); ck.add_argument("sandbox"); ck.add_argument("name", nargs="?")
    rb = sub.add_parser("rollback"); rb.add_argument("sandbox"); rb.add_argument("name")
    b = sub.add_parser("build", help="make a warm template: image -> layer -> prepared sandbox -> commit"); b.add_argument("name"); b.add_argument("--from", dest="base", help="OCI image, e.g. python:3.12-slim")
    b.add_argument("--run", action="append", default=[], help="a setup command (repeatable, must exit 0)"); b.add_argument("--allow", action="append", default=[], help="a host the setup commands may reach (repeatable)")
    b.add_argument("--mem", type=int, default=1024); b.add_argument("--file", action="append", default=[], help="LOCAL:REMOTE, copy a local file into the sandbox before the commands (repeatable)")
    k = sub.add_parser("keys"); ks = k.add_subparsers(dest="kcmd", required=True)
    ks.add_parser("ls"); kc = ks.add_parser("create"); kc.add_argument("name"); kc.add_argument("--role", choices=["member", "viewer"], default="member"); kc.add_argument("--days", type=float)
    ks.add_parser("revoke").add_argument("id")
    a = p.parse_args(argv)
    pr = lambda x: print(json.dumps(x, indent=2, ensure_ascii=False) if a.json else x, file=out)    # noqa: E731
    if a.cmd == "status":
        base = (a.url or os.environ.get("CAIRN_URL") or "http://127.0.0.1:18700").rstrip("/")
        try:
            with urllib.request.urlopen(base + "/status", timeout=10) as r: d = json.loads(r.read())
        except urllib.error.HTTPError as ex: d = json.loads(ex.read())
        if a.json: pr(d)
        else: print(d["status"], *[f"{k}={v}" for k, v in sorted(d["components"].items())], file=out)
        return 0 if d["status"] != "outage" else 2
    c_ = _client(a)
    try:
        if a.cmd == "whoami": me = c_.me(); pr(me if a.json else f"{me.get('user')}  account={me.get('account')}  role={me.get('role')}  projects={','.join(me.get('projects') or [])}" + ("  (API key)" if me.get("key") else ""))
        elif a.cmd == "ls":
            rows = [s.info for s in c_.sandboxes.list()]
            pr(rows) if a.json else _table(rows, ["id", "status", "template", "class", "node", "mem"], out)
        elif a.cmd == "create":
            net = {"mode": "allowlist", "allow_hosts": a.allow} if a.allow else None
            sb = c_.sandboxes.create(template=a.template, mem=a.mem, vcpu=a.vcpu, cls=a.cls, network=net); pr(sb.info if a.json else sb.id)
        elif a.cmd == "exec":
            cmd = a.command[1:] if a.command and a.command[0] == "--" else a.command
            if not cmd: p.error("exec: give a command after --")
            r = c_.sandboxes.get(a.sandbox).commands.run(" ".join(cmd), timeout=a.timeout)
            if a.json: pr(r.raw)
            else: out.write(r.stdout); sys.stderr.write(r.stderr)
            return r.exit if isinstance(r.exit, int) else 1
        elif a.cmd in ("pause", "resume"): getattr(c_.sandboxes.get(a.sandbox), a.cmd)(); print(f"{a.sandbox}: {a.cmd}d" if a.cmd == "pause" else f"{a.sandbox}: resumed", file=out)
        elif a.cmd == "rm": c_.sandboxes.get(a.sandbox).kill(); print(f"{a.sandbox}: deleted", file=out)
        elif a.cmd == "cp":
            (s_id, s_path), (d_id, d_path) = _split_remote(a.src), _split_remote(a.dst)
            if bool(s_id) == bool(d_id): p.error("cp: exactly one side is sandbox:/path")
            if d_id:
                n = c_.sandboxes.get(d_id).files.write(d_path, open(s_path, "rb").read()); print(f"{n} bytes -> {d_id}:{d_path}", file=out)
            else:
                data = c_.sandboxes.get(s_id).files.read(s_path); dst = os.path.join(d_path, os.path.basename(s_path)) if os.path.isdir(d_path) else d_path
                open(dst, "wb").write(data); print(f"{len(data)} bytes -> {dst}", file=out)
        elif a.cmd == "build":
            files = {}
            for spec in a.file:
                local, _, remote = spec.partition(":"); files[remote] = open(local).read()
            done = c_.templates.build(a.name, base=a.base, run=a.run, files=files, mem=a.mem, allow=a.allow, log=lambda m: print(m, file=out))
            pr(done if a.json else f"template {a.name} ready (build {done['id']}); use template_key=\"{a.name}\"")
        elif a.cmd == "checkpoint": pr(c_.sandboxes.get(a.sandbox).checkpoints.create(a.name))
        elif a.cmd == "rollback": c_.sandboxes.get(a.sandbox).checkpoints.rollback(a.name); print(f"{a.sandbox}: rolled back to {a.name}", file=out)
        elif a.cmd == "usage":
            proj = c_.project or (c_.me().get("projects") or ["default"])[0]; today = c_.billing.today(proj); st = c_.billing.statement(proj)
            pr({"project": proj, "today_usd": today, "total_usd": st.get("total_usd")} if a.json else f"{proj}: today ${today:.4f}, period ${st.get('total_usd', 0):.4f}")
        elif a.cmd == "keys":
            if a.kcmd == "ls": rows = c_.projects.keys(); pr(rows) if a.json else _table(rows, ["id", "name", "role", "expires", "last_used"], out)
            elif a.kcmd == "create":
                r = c_.projects.create_key(a.name, role=a.role, ttl_s=int(a.days * 86400) if a.days else None); pr(r if a.json else f"{r['key']['id']}\nsecret (shown once): {r['secret']}")
            else: c_.projects.revoke_key(a.id); print(f"{a.id}: revoked", file=out)
    except ApiError as ex:
        print(f"cairn: {ex.status}: {ex.message}", file=sys.stderr); return 1
    except RuntimeError as ex:                                              # a build step failed: its message already says which and why
        print(f"cairn: {ex}", file=sys.stderr); return 1
    return 0


def main(): sys.exit(run(sys.argv[1:]))


if __name__ == "__main__": main()
