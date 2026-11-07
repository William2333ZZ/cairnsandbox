#!/usr/bin/env python3
"""Egress policy: L7 rules, audited mode, credential injection, the decision log (docs/002 沙箱侧 6, D36).
usage: test-egress.py http://node [echo-host]   echo-host = an address the node's proxy can reach this process on (default: this host's IP)
Light: one 512 MiB VM. Starts a small HTTP echo server here that the sandbox is pointed at through the proxy."""
import json, os, socket, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
ECHO_HOST = sys.argv[2] if len(sys.argv) > 2 else socket.gethostbyname(socket.gethostname())
AUTH = {"Authorization": "Bearer " + os.environ["NODE_TOKEN"]} if os.environ.get("NODE_TOKEN") else {}

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=300) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]

class Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization"), "x": self.headers.get("X-Team")}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_POST = do_GET
    def log_message(self, *a): pass
srv = ThreadingHTTPServer(("0.0.0.0", 0), Echo); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
E = f"http://{ECHO_HOST}:{PORT}"
print(f"echo server at {E}")

print("== credentials ==")
c, r = req("PUT", "/egress/credentials/team-api", {"header": "Authorization", "value": "Bearer secret-123"}); step("credential stored (name listed, value not)", c == 200 and r["credentials"] == ["team-api"] and "secret" not in json.dumps(r))
c, r = req("PUT", "/egress/credentials/Bad!Name", {"header": "Authorization", "value": "x"}); step("bad credential name refused", c == 400)
c, r = req("GET", "/egress/credentials"); step("listed", r.get("credentials") == ["team-api"])

print("== rules ==")
rules = [{"name": "deny-admin", "match": {"scheme": "http", "host": ECHO_HOST, "path": "/admin/*"}, "action": {"allow": False}},
         {"name": "api-with-key", "match": {"scheme": "http", "host": ECHO_HOST, "method": ["GET"], "path": "/api/*"}, "action": {"allow": True, "inject": "team-api"}},
         {"name": "echo-rest", "match": {"host": ECHO_HOST}, "action": {"allow": True}}]
c, a = req("POST", "/sandboxes", {"class": "ephemeral", "ttl_idle_s": 300, "mem": 512, "network": {"mode": "closed", "rules": rules}}); A = a.get("id")
step("sandbox with rules (closed mode otherwise)", c == 201 and a["network"]["mode"] == "closed" and [x["name"] for x in a["network"]["rules"]] == ["deny-admin", "api-with-key", "echo-rest"], f"{A} {a.get('network')}")
c, r = req("POST", "/sandboxes", {"class": "ephemeral", "mem": 512, "network": {"rules": [{"name": "x", "match": {"scheme": "ftp"}, "action": {"allow": True}}]}}); step("bad rule refused at create", c == 400, str(r.get("error"))[:60])
curl = lambda path, extra="": ex(A, f"curl -s --max-time 8 {extra} {E}{path} || echo curl-exit-nonzero")["stdout"].strip()
r = curl("/api/items"); step("GET /api/* allowed with the credential injected, sandbox never saw the value", '"auth": "Bearer secret-123"' in r and "secret-123" not in ex(A, "env; cat /etc/hosts")["stdout"], r[:120])
r = curl("/api/items", "-X POST"); step("POST /api/* falls through to the plain allow rule: no injection", '"auth": null' in r, r[:80])
r = curl("/admin/users"); step("/admin/* denied by the first matching rule", "egress denied" in r or r == "", r[:80])
r = curl("/anything"); step("other paths on the echo host allowed by the catch-all rule", '"path": "/anything"' in r, r[:80])
r = ex(A, "curl -s --max-time 8 -o /dev/null -w %{http_code} http://example.com/")["stdout"].strip(); step("a host no rule names is closed", r.startswith("403"), r)
c, lg = req("GET", f"/sandboxes/{A}/egress"); rules_hit = [d.get("rule") for d in lg.get("decisions", [])]
step("decision log names the rule, the path and what was injected", "api-with-key" in rules_hit and "deny-admin" in rules_hit and any(d.get("injected") == ["Authorization"] for d in lg["decisions"]) and any(d.get("allowed") is False and d.get("rule") is None for d in lg["decisions"]), str(rules_hit))

print("== https upstream (D89) ==")
TLS_HOST = os.environ.get("TLS_HOST", "mirrors.aliyun.com")        # any HTTPS server the node can reach
c, n = req("PUT", f"/sandboxes/{A}/network", {"mode": "closed", "rules": [{"name": "tls-up", "match": {"scheme": "http", "host": TLS_HOST}, "action": {"allow": True, "upstream": "https"}}]})
step("rule with upstream https accepted", c == 200 and n["network"]["rules"][0]["action"].get("upstream") == "https", str(n)[:120])
r = ex(A, f"curl -s --max-time 15 -o /dev/null -w %{{http_code}} http://{TLS_HOST}/ || echo curl-exit-nonzero")["stdout"].strip()
c, lg = req("GET", f"/sandboxes/{A}/egress"); last = [d for d in lg.get("decisions", []) if d.get("host") == TLS_HOST][-1:]
step("plain http from the sandbox left the node over TLS (port 443, upstream https)", r[:1] in "23" and last and last[0].get("port") == 443 and last[0].get("upstream") == "https", f"{r} {last}")
c, n = req("PUT", f"/sandboxes/{A}/network", {"rules": [{"name": "x", "match": {"host": TLS_HOST}, "action": {"allow": True, "upstream": "https"}}]}); step("upstream without match.scheme http refused", c == 400)

print("== audited mode ==")
c, n = req("PUT", f"/sandboxes/{A}/network", {"mode": "audited", "rules": [rules[0]]}); step("switch to audited with one deny rule", c == 200 and n["network"]["mode"] == "audited")
r = ex(A, "curl -s --max-time 8 -o /dev/null -w %{http_code} http://example.com/ || echo curl-exit-nonzero")["stdout"].strip(); step("audited: unknown hosts pass (or fail upstream), never 403 from the proxy", r != "403", r)
r = curl("/admin/x"); step("the deny rule still applies in audited mode", "egress denied" in r or r == "")
c, lg = req("GET", f"/sandboxes/{A}/egress"); step("every request is in the log", len(lg["decisions"]) >= 7, str(len(lg["decisions"])))
c, n = req("PUT", f"/sandboxes/{A}/network", {"rules": [{"name": "bad", "match": {}, "action": {}}]}); step("bad rule refused on update", c == 400)

print("== cleanup ==")
req("DELETE", f"/sandboxes/{A}"); c, r = req("DELETE", "/egress/credentials/team-api"); step("credential dropped", c == 200 and r["credentials"] == [])
