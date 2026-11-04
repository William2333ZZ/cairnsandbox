"""The Python SDK against a fake control plane: paths, bodies, headers and error mapping follow CONTRACT §1 (sdk/python)."""
import base64, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "python"))
import pytest
import cairnbox as S

CALLS = []
BOXES = {}
FILES = {}


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _json(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _raw(self, code, data, ct="application/octet-stream"):
        self.send_response(code); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_DELETE(self): self.handle_any("DELETE")
    def handle_any(self, m):
        n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n) if n else b""
        path, _, qs = self.path.partition("?")
        body = json.loads(raw) if raw and self.headers.get("Content-Type", "").startswith("application/json") else raw
        CALLS.append((m, path, qs, body, self.headers.get("Authorization"), self.headers.get("X-Project")))
        parts = path.strip("/").split("/")
        if path == "/me": return self._json(200, {"user": "u", "account": "acme", "projects": ["acme"]})
        if path == "/sandboxes" and m == "POST":
            sid = f"sb-{len(BOXES) + 1:04d}"; BOXES[sid] = {"id": sid, "status": "running", "node": "na", **body}; return self._json(201, BOXES[sid])
        if path == "/sandboxes" and m == "GET": return self._json(200, {"sandboxes": list(BOXES.values())})
        if path == "/sandboxes/import": return self._json(201, {"id": "sb-imported", "status": "paused"})
        if parts[0] == "sandboxes" and len(parts) >= 2:
            sid = parts[1]; sb = BOXES.get(sid)
            if not sb: return self._json(404, {"error": f"no sandbox {sid}"})
            sub = "/".join(parts[2:])
            if sub == "" and m == "GET": return self._json(200, sb)
            if sub == "" and m == "DELETE": BOXES.pop(sid); return self._json(200, {"deleted": sid})
            if sub == "exec": return self._json(200, {"exit": 0 if "fail" not in body["cmd"] else 7, "stdout": f"ran:{body['cmd']}\n", "stderr": "", "timeout": False, "elapsed_s": 0.01})
            if sub == "exec/stream":
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                for line in (b'{"event":"stdout","data":"a"}\n', b'{"event":"stderr","data":"b"}\n', b'{"done":true,"exit":0,"timeout":false,"elapsed_s":0.1}\n'):
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                self.wfile.write(b"0\r\n\r\n"); return
            if sub == "files" and m == "PUT": FILES.setdefault(sid, {})[dict(x.split("=") for x in qs.split("&"))["path"]] = body; return self._json(200, {"bytes": len(body)})
            if sub == "files" and m == "GET":
                p = dict(x.split("=") for x in qs.split("&"))["path"]; d = FILES.get(sid, {}).get(p)
                return self._raw(200, d) if d is not None else self._json(404, {"error": "no file"})
            if sub in ("pause", "resume"): sb["status"] = "paused" if sub == "pause" else "running"; return self._json(200, sb)
            if sub == "ttl": sb.update(body); return self._json(200, sb)
            if sub == "checkpoints" and m == "POST": sb.setdefault("ckpts", []).append(body.get("name") or "c1"); return self._json(201, {"name": sb["ckpts"][-1], "chain_len": 1})
            if sub == "checkpoints" and m == "GET": return self._json(200, {"checkpoints": [{"name": c} for c in sb.get("ckpts", [])]})
            if sub == "rollback": return self._json(200, sb) if body["checkpoint"] in sb.get("ckpts", []) else self._json(404, {"error": "no checkpoint"})
            if sub == "fork":
                clones = [{"id": f"{sid}-f{i}", "status": "running", "class": body.get("class", "ephemeral")} for i in range(body.get("n", 1))]
                for c in clones: BOXES[c["id"]] = c
                return self._json(201, {"clones": clones, "checkpoint": body.get("checkpoint")})
            if sub == "network" and m == "PUT":
                if body.get("mode") == "audited": return self._json(202, {"request": {"id": "nr-1", "status": "pending", "network": body}})
                sb["network"] = {**sb.get("network", {}), **body}; return self._json(200, {"network": sb["network"]})
            if sub == "budget": return self._json(200, {"budget": body})
            if sub == "events": return self._json(200, {"events": [{"seq": 1, "type": "report", "data": {"state": "progress"}}, {"seq": 2, "type": "note", "data": {}}, {"seq": 3, "type": "report", "data": {"state": "ready", "text": "done"}}], "last_seq": 3})
            if sub == "sessions" and m == "POST": return self._json(201, {"session": "s1", "pty": body.get("pty", False)})
            if parts[2] == "sessions" and parts[-1] == "read": return self._json(200, {"data_b64": base64.b64encode(b"prompt$ ").decode(), "bytes": 8})
            if parts[2] == "sessions" and parts[-1] == "write": return self._json(200, {"ok": True})
            if parts[2] == "sessions" and m == "DELETE": return self._json(200, {"closed": True})
            if parts[2] == "ports": return self._raw(200, b"<h1>hi</h1>", "text/html")
        if path == "/volumes" and m == "POST": return self._json(201, {"id": "vol-1", **body})
        if path == "/volumes/from-archive": return self._json(201, {"id": "vol-2", "name": dict(x.split("=") for x in qs.split("&"))["name"], "content_bytes": len(body)})
        if path == "/metering/projects/acme/today": return self._json(200, {"spent_usd": 0.5})
        if path == "/metering/projects/acme/statement" and "format=csv" in qs: return self._raw(200, b"project,from,to,kind\n", "text/csv")
        if path == "/metering/projects/acme/statement": return self._json(200, {"project": "acme", "lines": [], "total_usd": 0})
        if path == "/metering/sandboxes/" + parts[-1] if len(parts) > 2 else False: return self._json(200, {"usage": []})
        if path == "/network/requests": return self._json(200, {"requests": [{"id": "nr-1", "status": "pending"}]})
        if path == "/network/requests/nr-1/approve": return self._json(200, {"request": {"id": "nr-1", "status": "approved"}})
        if path == "/quota-full": return self._json(402, {"error": "quota: daily budget"})
        if path == "/full": return self._json(429, {"error": "no node with capacity"})
        self._json(404, {"error": "no route " + path})


@pytest.fixture(scope="module")
def cp():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_sends_bearer_and_project_and_maps_errors(cp):
    c = S.Client(cp, token="tok", project="acme")
    assert c.me()["account"] == "acme" and CALLS[-1][4] == "Bearer tok" and CALLS[-1][5] == "acme"
    with pytest.raises(S.NotFound): c.sandboxes.get("sb-nope")
    with pytest.raises(S.QuotaExceeded): c.get("/quota-full")
    with pytest.raises(S.NoCapacity) as e: c.get("/full")
    assert e.value.status == 429 and "capacity" in str(e.value)
    with pytest.raises(S.ApiError) as e: S.Client("http://127.0.0.1:1", token="x").health()
    assert e.value.status == 0 and "transport" in str(e.value)


def test_sandbox_lifecycle_commands_files_checkpoints_fork(cp):
    c = S.Client(cp, token="tok")
    sb = c.sandboxes.create(mem=256, cls="persistent", network={"allow_hosts": ["pypi.org"]}, env={"A": "1"}, priority="interactive", retain_paused_s=3600)
    assert sb.id.startswith("sb-") and sb.status == "running" and CALLS[-1][3]["class"] == "persistent" and CALLS[-1][3]["env"] == {"A": "1"} and "vcpu" not in CALLS[-1][3]
    r = sb.commands.run("echo hi", cwd="/tmp", timeout=5, user="root")
    assert r.exit == 0 and r.stdout == "ran:echo hi\n" and CALLS[-1][3] == {"cmd": "echo hi", "cwd": "/tmp", "timeout": 5, "user": "root"}
    with pytest.raises(RuntimeError): sb.commands.run("fail now").check()
    assert list(sb.commands.stream("ls")) [:2] == [("stdout", "a"), ("stderr", "b")]
    done = list(sb.commands.stream("ls"))[-1]; assert done[0] == "done" and done[1].exit == 0
    assert sb.files.write("/home/agent/f.txt", "héllo") == 6 and sb.files.read_text("/home/agent/f.txt") == "héllo" and CALLS[-2][2] == "path=/home/agent/f.txt"
    with pytest.raises(S.NotFound): sb.files.read("/nope")
    assert sb.pause().status == "paused" and sb.resume().status == "running"
    assert sb.set_timeout(ttl_idle_s=60, retain_paused_s=10).info["ttl_idle_s"] == 60 and CALLS[-1][3] == {"ttl_idle_s": 60, "retain_paused_s": 10}
    assert sb.checkpoints.create("c1", note="n")["name"] == "c1" and [x["name"] for x in sb.checkpoints.list()] == ["c1"]
    assert sb.checkpoints.rollback("c1")["id"] == sb.id
    with pytest.raises(S.NotFound): sb.checkpoints.rollback("nope")
    clones = sb.fork(n=2, checkpoint="c1", cls="persistent", ttl_idle_s=30)
    assert len(clones) == 2 and clones[0].id == f"{sb.id}-f0" and CALLS[-1][3] == {"n": 2, "checkpoint": "c1", "class": "persistent", "ttl_idle_s": 30}
    assert sb.budget(max_usd=1.5) == {"max_usd": 1.5} and sb.events()[0][0]["type"] == "report"
    assert sb.wait_for_report(timeout=5)["data"]["state"] == "ready"                       # the progress report before it does not end the wait
    assert sb.wait_for_report(timeout=5, include_progress=True)["data"]["state"] == "progress"
    with sb.pty.open(pty=True) as sess:
        sess.write("ls\n"); assert sess.read(0.1) == b"prompt$ " and sess.pty is True
    assert CALLS[-1][0] == "DELETE" and CALLS[-1][1].endswith("/sessions/s1")
    assert sb.ports.url(8080, "x") == f"{cp}/sandboxes/{sb.id}/ports/8080/x" and sb.ports.get(8080)[0] == 200 and b"hi" in sb.ports.get(8080)[2]
    assert sb.network.set(allow_hosts=[])["allow_hosts"] == []
    with pytest.raises(S.NeedsApproval) as e: sb.network.set(mode="audited")
    assert e.value.request["id"] == "nr-1" and c.network.requests()[0]["status"] == "pending" and c.network.approve("nr-1")["status"] == "approved"
    with S.Sandbox(c, sb.info) as same: assert same.id == sb.id
    assert CALLS[-1][0] == "DELETE" and sb.id not in BOXES                               # the context manager killed it
    assert c.sandboxes.import_bundle("sb-x-1").status == "paused"


def test_volumes_builds_and_billing_shapes(cp):
    c = S.Client(cp, token="tok", project="acme")
    assert c.volumes.create("data", size_mib=256)["id"] == "vol-1" and CALLS[-1][3] == {"name": "data", "size_mib": 256}
    v = c.volumes.from_archive("home", b"TARBYTES", size_mib=64); assert v["content_bytes"] == 8 and CALLS[-1][2] == "name=home&size_mib=64" and CALLS[-1][3] == b"TARBYTES"
    assert c.billing.today() == 0.5 and c.billing.statement_csv().startswith("project,from,to")
    assert c.billing.statement(since=1, until=2) is not None and CALLS[-1][2] == "from=1&to=2"


def test_mcp_server_lists_and_calls_tools_and_reports_api_errors_as_tool_errors(cp):
    from cairnbox import mcp
    srv = mcp.Server(S.Client(cp, token="tok"))
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL and "tools" in init["result"]["capabilities"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None and srv.initialized
    names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert {"sandbox_create", "sandbox_run", "sandbox_checkpoint", "sandbox_rollback", "sandbox_fork", "sandbox_kill"} <= set(names)
    def call(tool, **args): return srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": tool, "arguments": args}})["result"]
    created = json.loads(call("sandbox_create", mem=256, env={"A": "1"})["content"][0]["text"]); sid = created["id"]
    assert sid.startswith("sb-") and CALLS[-1][3]["env"] == {"A": "1"}
    run = json.loads(call("sandbox_run", sandbox=sid, cmd="echo hi")["content"][0]["text"]); assert run["exit"] == 0 and run["stdout"] == "ran:echo hi\n"
    assert json.loads(call("sandbox_write_file", sandbox=sid, path="/home/agent/a.txt", text="hey")["content"][0]["text"]) == {"bytes": 3}
    assert call("sandbox_read_file", sandbox=sid, path="/home/agent/a.txt")["content"][0]["text"] == "hey"
    assert json.loads(call("sandbox_checkpoint", sandbox=sid, name="c1")["content"][0]["text"])["name"] == "c1"
    assert json.loads(call("sandbox_fork", sandbox=sid, checkpoint="c1")["content"][0]["text"]) == [f"{sid}-f0"]
    bad = call("sandbox_rollback", sandbox=sid, checkpoint="nope"); assert bad["isError"] and "404" in bad["content"][0]["text"]
    assert json.loads(call("sandbox_kill", sandbox=sid)["content"][0]["text"]) == {"deleted": sid}
    unknown = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "nope"}); assert unknown["error"]["code"] == -32601
    import io
    out = io.StringIO(); srv.serve_stdio(io.StringIO('{"jsonrpc":"2.0","id":5,"method":"ping"}\n\nnot json\n'), out)
    assert json.loads(out.getvalue())["result"] == {}


def test_mcp_names_a_missing_required_argument_instead_of_a_keyerror(cp):
    from cairnbox import mcp
    srv = mcp.Server(S.Client(cp, token="tok"))
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sandbox_run", "arguments": {"cmd": "ls"}}})["result"]
    assert r["isError"] and "missing required argument(s): sandbox" in r["content"][0]["text"]
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})["result"]
    assert r["isError"] and "unknown tool" in r["content"][0]["text"]


def test_ports_and_websocket_carry_the_project_header(cp):
    c = S.Client(cp, token="tok", project="acme")
    sb = c.sandboxes.create(mem=128)
    assert sb.ports.get(8080)[0] == 200 and CALLS[-1][5] == "acme"
    sb.kill()
