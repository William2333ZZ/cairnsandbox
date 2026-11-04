"""The `aios` command line against the SDK's fake control plane."""
import io, json, os, sys, threading
from http.server import ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "python"))
import pytest
import test_sdk
from cairnbox import cli


BUILDS = []


class Fake2(test_sdk.Fake):
    def handle_any(self, m):
        path = self.path.partition("?")[0]
        if path == "/status": return self._json(200, {"status": "degraded", "components": {"api": "operational", "sandboxes": "degraded"}})
        if path == "/builds" and m == "POST":
            n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n)); BUILDS.append(b); return self._json(201, {"id": f"b-{len(BUILDS)}", "kind": b["kind"], "status": "running"})
        if path.startswith("/builds/b-") and m == "GET":
            i = int(path.rsplit("-", 1)[1]); b = BUILDS[i - 1]
            return self._json(200, {"id": f"b-{i}", "status": "failed" if b.get("image") == "nope:latest" else "done", "error": "registry: 404" if b.get("image") == "nope:latest" else "", "result": {"id": "L-fake"} if b["kind"] == "oci" else {"key": b.get("name")}})
        if path == "/projects/acme/keys" and m == "GET": return self._json(200, {"keys": [{"id": "key-1", "name": "ci", "role": "viewer", "expires": 0, "last_used": 0}]})
        if path == "/projects/acme/keys" and m == "POST":
            n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n)); return self._json(201, {"key": {"id": "key-2", **b}, "secret": "cairn_secret"})
        if path == "/projects/acme/keys/key-1" and m == "DELETE": return self._json(200, {"revoked": "key-1"})
        return super().handle_any(m)


@pytest.fixture()
def cp():
    test_sdk.BOXES.clear(); test_sdk.FILES.clear(); test_sdk.CALLS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake2); threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown(); srv.server_close()


def aios(cp, *argv):
    out = io.StringIO(); rc = cli.run(["--url", cp, "--token", "tok", "--project", "acme", *argv], out); return rc, out.getvalue()


def test_create_list_exec_pause_and_remove(cp):
    rc, sid = aios(cp, "create", "--mem", "256", "--allow", "pypi.org"); sid = sid.strip()
    assert rc == 0 and sid.startswith("sb-")
    body = [c for c in test_sdk.CALLS if c[0] == "POST" and c[1] == "/sandboxes"][0][3]
    assert body["mem"] == 256 and body["network"] == {"mode": "allowlist", "allow_hosts": ["pypi.org"]}
    assert sid in aios(cp, "ls")[1]
    rc, o = aios(cp, "exec", sid, "--", "echo", "hi"); assert rc == 0 and "ran:echo hi" in o
    assert aios(cp, "exec", sid, "--", "fail")[0] == 7                                   # the command's exit code is the CLI's
    assert "paused" in aios(cp, "pause", sid)[1]; assert "deleted" in aios(cp, "rm", sid)[1]
    assert sid not in aios(cp, "ls")[1]


def test_cp_both_ways_and_errors(cp, tmp_path):
    sid = aios(cp, "create")[1].strip(); f = tmp_path / "a.txt"; f.write_text("hello")
    assert "5 bytes" in aios(cp, "cp", str(f), f"{sid}:/home/agent/a.txt")[1]
    assert aios(cp, "cp", f"{sid}:/home/agent/a.txt", str(tmp_path))[0] == 0 and (tmp_path / "a.txt").read_text() == "hello"
    assert aios(cp, "exec", "sb-nope", "--", "true")[0] == 1                             # a 404 is a message and exit 1, not a traceback


def test_keys_status_and_checkpoints(cp):
    assert "ci" in aios(cp, "keys", "ls")[1]
    rc, o = aios(cp, "keys", "create", "dash", "--role", "viewer", "--days", "1"); assert rc == 0 and "cairn_secret" in o
    assert "revoked" in aios(cp, "keys", "revoke", "key-1")[1]
    out = io.StringIO(); assert cli.run(["--url", cp, "status"], out) == 0 and "degraded" in out.getvalue()
    sid = aios(cp, "create")[1].strip(); aios(cp, "checkpoint", sid, "mk"); assert "rolled back" in aios(cp, "rollback", sid, "mk")[1]


def test_build_makes_a_template_from_an_image_runs_the_setup_and_cleans_up(cp, tmp_path):
    BUILDS.clear(); (tmp_path / "req.txt").write_text("pandas\n")
    rc, out = aios(cp, "build", "py-data", "--from", "python:3.12-slim", "--run", "echo setup", "--allow", "pypi.org", "--file", f"{tmp_path}/req.txt:/home/agent/req.txt")
    assert rc == 0 and "template py-data ready" in out and "$ echo setup" in out, out
    kinds = [b["kind"] for b in BUILDS]; assert kinds == ["oci", "commit"] and BUILDS[0]["image"] == "python:3.12-slim" and BUILDS[1]["name"] == "py-data"
    create = [c for c in test_sdk.CALLS if c[0] == "POST" and c[1] == "/sandboxes"][-1][3]
    assert create["layers"] == ["L-fake"] and create["network"] == {"mode": "allowlist", "allow_hosts": ["pypi.org"]} and create["class"] == "persistent"
    assert not test_sdk.BOXES, "the preparation sandbox must be thrown away"


def test_a_failing_setup_command_stops_the_build_shows_why_and_still_cleans_up(cp):
    BUILDS.clear()
    rc, out = aios(cp, "build", "bad", "--run", "fail-now")
    assert rc == 1 and not test_sdk.BOXES and [b["kind"] for b in BUILDS] == []          # no image: straight to the sandbox; the command's non-zero exit aborts before any commit
    rc, out = aios(cp, "build", "x", "--from", "nope:latest")
    assert rc == 1                                                                         # a failed image build is a message, not a traceback
