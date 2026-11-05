#!/usr/bin/env python3
"""E2B conformance: the OFFICIAL e2b Python SDK against a Cairn control plane's E2B surface, case by case.

    pip install e2b
    E2B_API_URL=http://127.0.0.1:18710 E2B_SANDBOX_URL=http://127.0.0.1:18710 E2B_API_KEY=<member or admin token> \\
        python3 ops/e2b-conformance.py [--template debian-13] [--json out.json] [--only commands,files]

Needs sandboxes whose base image carries the envd-capable sandboxd (docs/guide/api/e2b.md). Every case runs in its own try block and is reported as
PASS / FAIL / SKIP with the reason; nothing here is a benchmark and nothing is mocked. The exit code is the number of FAILs (SKIPs do not count), so the
matrix in docs/guide/api/e2b.md is whatever this printed, not what we hope.
Optional (our own extension, needs CAIRN_URL + CAIRN_TOKEN): processes survive checkpoint + rollback and can be re-attached with commands.connect.
"""
import argparse
import importlib.metadata
import json
import os
import sys
import time
import traceback

from e2b import PtySize, Sandbox
from e2b.exceptions import NotFoundException

CASES = []


def case(group, name):
    def deco(fn):
        CASES.append((group, name, fn))
        return fn
    return deco


class Skip(Exception):
    pass


def need(cond, why):
    if not cond:
        raise Skip(why)


# ---------------------------------------------------------------- commands
@case("commands", "run: stdout, exit code 0")
def _(sb):
    r = sb.commands.run("echo hello; echo -n world")
    assert r.stdout == "hello\nworld", r.stdout
    assert r.exit_code == 0


@case("commands", "run: stderr is separate")
def _(sb):
    r = sb.commands.run("echo out; echo err >&2")
    assert r.stdout == "out\n" and r.stderr == "err\n", (r.stdout, r.stderr)


@case("commands", "run: non-zero exit raises CommandExitException with the code")
def _(sb):
    from e2b import CommandExitException
    try:
        sb.commands.run("exit 7")
    except CommandExitException as e:
        assert e.exit_code == 7, e.exit_code
        return
    raise AssertionError("no exception")


@case("commands", "run: streaming callbacks deliver output while it is produced")
def _(sb):
    got, t = [], []
    t0 = time.time()
    sb.commands.run("echo a; sleep 1; echo b", on_stdout=lambda d: (got.append(d), t.append(time.time() - t0)))
    assert "".join(got) == "a\nb\n", got
    assert len(t) >= 2 and t[-1] - t[0] > 0.5, f"output was not streamed: {t}"


@case("commands", "run: envs and cwd")
def _(sb):
    r = sb.commands.run("echo $FOO; pwd", envs={"FOO": "bar"}, cwd="/tmp")
    assert r.stdout == "bar\n/tmp\n", r.stdout


@case("commands", "run: user=root")
def _(sb):
    r = sb.commands.run("id -un", user="root")
    assert r.stdout.strip() == "root", r.stdout


@case("commands", "run: default user is unprivileged")
def _(sb):
    r = sb.commands.run("id -u")
    assert r.stdout.strip() != "0", "default user is root"


@case("commands", "run: command not found is exit 127, not a transport error")
def _(sb):
    from e2b import CommandExitException
    try:
        sb.commands.run("definitely-not-a-command")
    except CommandExitException as e:
        assert e.exit_code == 127
        return
    raise AssertionError("no exception")


@case("commands", "background: handle, list, kill")
def _(sb):
    h = sb.commands.run("sleep 60", background=True)
    pids = [p.pid for p in sb.commands.list()]
    assert h.pid in pids, (h.pid, pids)
    assert sb.commands.kill(h.pid) is True
    time.sleep(0.5)
    assert h.pid not in [p.pid for p in sb.commands.list()]


@case("commands", "stdin: send_stdin to a stdin-enabled process")
def _(sb):
    h = sb.commands.run("cat", background=True, stdin=True)
    sb.commands.send_stdin(h.pid, "ping\n")
    time.sleep(0.5)
    sb.commands.close_stdin(h.pid)
    r = h.wait()
    assert "ping" in r.stdout, r.stdout


@case("commands", "timeout: a long command is cut by the client timeout, the sandbox lives")
def _(sb):
    from e2b import TimeoutException
    try:
        sb.commands.run("sleep 30", timeout=2)
    except TimeoutException:
        pass
    else:
        raise AssertionError("no timeout")
    assert sb.commands.run("echo alive").stdout == "alive\n"


@case("commands", "connect: re-attach to a running background command")
def _(sb):
    h = sb.commands.run("sh -c 'echo first; sleep 2; echo second'", background=True)
    time.sleep(0.5)
    h2 = sb.commands.connect(h.pid)
    r = h2.wait()
    assert "second" in r.stdout, r.stdout


# ---------------------------------------------------------------- pty
@case("pty", "create, send input, read output, resize, kill")
def _(sb):
    out = []
    h = sb.pty.create(PtySize(80, 24))
    sb.pty.send_stdin(h.pid, b"echo pty-ok\n")
    time.sleep(0.5)
    sb.pty.resize(h.pid, PtySize(100, 30))
    sb.pty.send_stdin(h.pid, b"exit\n")
    h.wait(on_pty=lambda d: out.append(bytes(d)))     # output is read through the handle
    assert b"pty-ok" in b"".join(out), out
    h2 = sb.pty.create(PtySize(80, 24))
    assert sb.pty.kill(h2.pid) is True


# ---------------------------------------------------------------- files
@case("files", "write + read text and bytes")
def _(sb):
    info = sb.files.write("/tmp/e2b/a.txt", "héllo")
    assert info.path == "/tmp/e2b/a.txt", info
    assert sb.files.read("/tmp/e2b/a.txt") == "héllo"
    sb.files.write("/tmp/e2b/b.bin", bytes(range(256)))
    assert sb.files.read("/tmp/e2b/b.bin", format="bytes") == bytes(range(256))


@case("files", "write_files (several at once)")
def _(sb):
    sb.files.write_files([{"path": "/tmp/e2b/m1.txt", "data": "1"}, {"path": "/tmp/e2b/m2.txt", "data": "2"}])
    assert sb.files.read("/tmp/e2b/m1.txt") == "1" and sb.files.read("/tmp/e2b/m2.txt") == "2"


@case("files", "a file written by the SDK is visible to commands and vice versa")
def _(sb):
    sb.files.write("/tmp/e2b/c.txt", "from sdk")
    assert sb.commands.run("cat /tmp/e2b/c.txt").stdout == "from sdk"
    sb.commands.run("echo -n from-shell > /tmp/e2b/d.txt")
    assert sb.files.read("/tmp/e2b/d.txt") == "from-shell"


@case("files", "read of a missing file raises NotFoundException")
def _(sb):
    try:
        sb.files.read("/tmp/e2b/nope")
    except NotFoundException:
        return
    raise AssertionError("no exception")


@case("files", "list, exists, get_info")
def _(sb):
    sb.files.write("/tmp/e2b/list/x.txt", "x")
    names = sorted(e.name for e in sb.files.list("/tmp/e2b/list"))
    assert names == ["x.txt"], names
    assert sb.files.exists("/tmp/e2b/list/x.txt") and not sb.files.exists("/tmp/e2b/list/y.txt")
    i = sb.files.get_info("/tmp/e2b/list/x.txt")
    assert i.size == 1 and i.name == "x.txt", i


@case("files", "make_dir (True first, False when it exists), rename, remove")
def _(sb):
    assert sb.files.make_dir("/tmp/e2b/newdir") is True
    assert sb.files.make_dir("/tmp/e2b/newdir") is False
    sb.files.write("/tmp/e2b/newdir/f", "1")
    sb.files.rename("/tmp/e2b/newdir/f", "/tmp/e2b/newdir/g")
    assert sb.files.exists("/tmp/e2b/newdir/g") and not sb.files.exists("/tmp/e2b/newdir/f")
    sb.files.remove("/tmp/e2b/newdir")
    assert not sb.files.exists("/tmp/e2b/newdir")


@case("files", "watch_dir sees a created file")
def _(sb):
    sb.files.make_dir("/tmp/e2b/watch")
    w = sb.files.watch_dir("/tmp/e2b/watch")
    sb.files.write("/tmp/e2b/watch/new.txt", "1")
    time.sleep(0.5)
    ev = w.get_new_events()
    w.stop()
    assert any(e.name == "new.txt" for e in ev), ev


@case("files", "a 20 MiB upload and download round-trips intact")
def _(sb):
    data = os.urandom(20 << 20)
    sb.files.write("/tmp/e2b/big.bin", data)
    assert sb.files.read("/tmp/e2b/big.bin", format="bytes") == data


# ---------------------------------------------------------------- sandbox lifecycle
@case("sandbox", "get_info, set_timeout, is_running")
def _(sb):
    assert sb.is_running()
    sb.set_timeout(600)
    assert sb.get_info().sandbox_id == sb.sandbox_id


@case("sandbox", "get_metrics answers")
def _(sb):
    sb.get_metrics()


@case("sandbox", "pause then connect resumes with files intact")
def _(sb):
    sb.files.write("/tmp/e2b/p.txt", "kept")
    sb.pause()
    sb2 = Sandbox.connect(sb.sandbox_id)
    assert sb2.files.read("/tmp/e2b/p.txt") == "kept"
    assert sb2.commands.run("echo back").stdout == "back\n"


@case("sandbox", "envs passed at create are visible to commands")
def _(_sb):
    s = Sandbox.create(template=TEMPLATE, envs={"CREATE_ENV": "yes"}, timeout=120)
    try:
        assert s.commands.run("echo $CREATE_ENV").stdout == "yes\n"
    finally:
        s.kill()


@case("sandbox", "kill, then the sandbox is gone")
def _(_sb):
    s = Sandbox.create(template=TEMPLATE, timeout=120)
    s.kill()
    time.sleep(0.5)
    assert not s.is_running()


@case("sandbox", "snapshot: create, start a new sandbox from it, list, delete")
def _(sb):
    sb.files.write("/tmp/e2b/snap.txt", "in snapshot")
    snap = sb.create_snapshot()
    try:
        s2 = Sandbox.create(template=snap.snapshot_id, timeout=120)
        try:
            assert s2.files.read("/tmp/e2b/snap.txt") == "in snapshot"
        finally:
            s2.kill()
        ids = [x.snapshot_id for x in Sandbox.list_snapshots().next_items()]
        assert snap.snapshot_id in ids, ids
    finally:
        assert Sandbox.delete_snapshot(snap.snapshot_id)


@case("sandbox", "fork (E2B control-plane call)")
def _(sb):
    need(hasattr(sb, "fork") or hasattr(Sandbox, "fork"), "this SDK version has no fork")
    c = sb.fork() if hasattr(sb, "fork") else Sandbox.fork(sb.sandbox_id)
    try:
        c = c[0] if isinstance(c, (list, tuple)) else c
        assert c.sandbox_id != sb.sandbox_id
    finally:
        try:
            (c.kill())
        except Exception:
            pass


def native(method, path, body=None):
    """Our own API, for the cases that check what the E2B surface did to it."""
    need(os.environ.get("CAIRN_URL") and os.environ.get("CAIRN_TOKEN"), "set CAIRN_URL and CAIRN_TOKEN to check the native API")
    import urllib.request
    req = urllib.request.Request(os.environ["CAIRN_URL"].rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ['CAIRN_TOKEN']}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=300))


@case("sandbox", "list: metadata and state filters")
def _(_sb):
    from e2b import SandboxQuery, SandboxState
    s = Sandbox.create(template=TEMPLATE, metadata={"conf": "list1"}, timeout=120)
    try:
        items = Sandbox.list(query=SandboxQuery(metadata={"conf": "list1"})).next_items()
        assert [i.sandbox_id for i in items] == [s.sandbox_id], items
        assert Sandbox.list(query=SandboxQuery(metadata={"conf": "list1"}, state=[SandboxState.PAUSED])).next_items() == []
    finally:
        s.kill()


@case("sandbox", "update_network: allow_out becomes an allowlist, deny_out closes it")
def _(sb):
    sb.update_network({"allow_out": ["pypi.org"]})
    net = native("GET", f"/sandboxes/{sb.sandbox_id}").get("network") or {}
    assert net.get("mode") == "allowlist" and "pypi.org" in net.get("allow_hosts", []), net
    sb.update_network({"deny_out": ["0.0.0.0/0"]})
    assert (native("GET", f"/sandboxes/{sb.sandbox_id}").get("network") or {}).get("mode") == "closed"


# ---------------------------------------------------------------- our extension
@case("cairn", "a background process survives checkpoint + rollback and can be re-attached")
def _(sb):
    need(os.environ.get("CAIRN_URL") and os.environ.get("CAIRN_TOKEN"), "set CAIRN_URL and CAIRN_TOKEN to run the native-API part")
    import urllib.request
    base, tok = os.environ["CAIRN_URL"].rstrip("/"), os.environ["CAIRN_TOKEN"]

    def native(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode(), headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=300))

    h = sb.commands.run("sh -c 'while true; do date +%s; sleep 1; done'", background=True)
    time.sleep(1.5)
    native("POST", f"/sandboxes/{sb.sandbox_id}/checkpoints", {"name": "cp1"})
    time.sleep(1.5)
    native("POST", f"/sandboxes/{sb.sandbox_id}/rollback", {"checkpoint": "cp1"})
    assert h.pid in [p.pid for p in sb.commands.list()], "the process did not come back with the VM memory"
    from e2b import TimeoutException
    seen = []
    h2 = sb.commands.connect(h.pid, timeout=4)
    try:
        h2.wait(on_stdout=lambda d: seen.append(d))
    except TimeoutException:
        pass
    sb.commands.kill(h.pid)
    assert seen, "no output after re-attach"


TEMPLATE = "base"


def main():
    global TEMPLATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default=os.environ.get("E2B_TEMPLATE", "base"))
    ap.add_argument("--json")
    ap.add_argument("--only", default="", help="comma-separated groups: commands,pty,files,sandbox,cairn")
    a = ap.parse_args()
    TEMPLATE = a.template
    only = {g for g in a.only.split(",") if g}
    for k in ("E2B_API_URL", "E2B_API_KEY"):
        if not os.environ.get(k):
            sys.exit(f"{k} is required")
    sb = Sandbox.create(template=TEMPLATE, timeout=900)
    results = []
    try:
        for group, name, fn in CASES:
            if only and group not in only:
                continue
            t0 = time.time()
            try:
                fn(sb)
                st, why = "PASS", ""
            except Skip as e:
                st, why = "SKIP", str(e)
            except Exception as e:  # noqa: BLE001
                st, why = "FAIL", f"{type(e).__name__}: {str(e)[:300]}"
                if os.environ.get("E2B_TRACE"):
                    traceback.print_exc()
            results.append({"group": group, "case": name, "status": st, "why": why, "seconds": round(time.time() - t0, 2)})
            print(f"{st:5} {group:9} {name}" + (f"  -- {why}" if why else ""), flush=True)
    finally:
        try:
            sb.kill()
        except Exception:  # noqa: BLE001
            pass
    n = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "SKIP")}
    ver = importlib.metadata.version("e2b")
    print(f"\ne2b {ver}: {n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped, of {len(results)}")
    if a.json:
        json.dump({"e2b_version": ver, "api_url": os.environ["E2B_API_URL"], "summary": n, "results": results}, open(a.json, "w"), indent=1)
    sys.exit(n["FAIL"])


if __name__ == "__main__":
    main()
