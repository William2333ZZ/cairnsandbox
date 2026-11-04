#!/usr/bin/env python3
"""Port exposure: a service an agent starts inside the sandbox is reachable through the node (and the control plane) at /sandboxes/<id>/ports/<port>/...
(docs/002 沙箱侧 8, D39). usage: test-ports.py http://node <langs-layer-id>   (the agent-langs layer brings a python3 to serve with)"""
import json, urllib.parse, os, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
LANGS = sys.argv[2] if len(sys.argv) > 2 else ""
AUTH = {"Authorization": "Bearer " + os.environ["NODE_TOKEN"]} if os.environ.get("NODE_TOKEN") else {}

def req(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            b = resp.read(); return resp.status, (b if raw else json.loads(b))
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (b if raw else (json.loads(b) if b[:1] == b"{" else {"raw": b[:200].decode(errors="replace")}))

import atexit
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))
def ex(sid, cmd, **kw): return req("POST", f"/sandboxes/{sid}/exec", {"cmd": cmd, **kw})[1]

spec = {"class": "ephemeral", "ttl_idle_s": 300, "mem": 512, "layers": [LANGS] if LANGS else []}
c, a = req("POST", "/sandboxes", spec); A = a["id"]; step("create", c == 201, A)
py = "/aios/runtime/python/bin/python3" if LANGS else "python3"
# not `-m http.server`: HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that hangs for minutes in a sandbox whose egress is closed (no resolver answers)
SERVER = "import socketserver, http.server as h; socketserver.TCPServer.allow_reuse_address = True; socketserver.TCPServer((\"0.0.0.0\", 8080), h.SimpleHTTPRequestHandler).serve_forever()"
# a bare Upgrade echo on 8090: answers 101 to a websocket handshake and then echoes raw bytes (no framing: the tunnel is byte-transparent, that is what is tested)
WS = """import socketserver
class H(socketserver.BaseRequestHandler):
    def handle(s):
        b = b''
        while b'\\r\\n\\r\\n' not in b:
            d = s.request.recv(4096)
            if not d: return
            b += d
        if b'Upgrade: websocket' not in b:
            s.request.sendall(b'HTTP/1.1 426 Upgrade Required\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n'); return
        s.request.sendall(b'HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\n\\r\\n')
        while True:
            d = s.request.recv(4096)
            if not d: return
            s.request.sendall(b'echo:' + d)
socketserver.TCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(('0.0.0.0', 8090), H).serve_forever()
"""
def put_raw(path, data):
    r = urllib.request.Request(BASE + f"/sandboxes/{A}/files?path={urllib.parse.quote(path)}", data=data, method="PUT", headers={"Content-Type": "application/octet-stream", **AUTH})
    with urllib.request.urlopen(r, timeout=60) as resp: return resp.status
r = ex(A, f"mkdir -p /home/agent/www/sub && echo '<h1>served from {A}</h1>' > /home/agent/www/index.html && echo a > /home/agent/www/sub/a.txt && cd /home/agent/www && setsid -f {py} -c '{SERVER}' >/tmp/srv.log 2>&1 < /dev/null; sleep 1.5; cat /tmp/srv.log | head -2; echo started")   # setsid: the server must outlive the exec's process group
step("a web server started inside the sandbox", "started" in r.get("stdout", ""), r.get("stdout", "").strip())
time.sleep(1.5)
c, body = req("GET", f"/sandboxes/{A}/ports/8080/index.html", raw=True)
step("reachable through the node at /ports/<port>/", c == 200 and f"served from {A}".encode() in body, f"{c} {body[:60]!r}")
c, body = req("GET", f"/sandboxes/{A}/ports/8080/sub/?x=1", raw=True); step("query strings pass through, directory listing served", c == 200 and b"a.txt" in body, f"{c} {body[:60]!r}")
c, r = req("GET", f"/sandboxes/{A}/ports/8081/"); step("a closed port answers 502 from the node, not a hang", c == 502, str(r.get("error"))[:60])

print("== websocket (Upgrade) tunnel ==")
import base64, socket as _socket
put_raw("/tmp/ws.py", WS.encode()); r = ex(A, f"setsid -f {py} /tmp/ws.py >/tmp/ws.log 2>&1 </dev/null; sleep 1.5; cat /tmp/ws.log; echo started")
step("websocket echo server started inside the sandbox", "started" in r.get("stdout", "") and "Error" not in r.get("stdout", ""), r.get("stdout", "").strip()[:80])
u = urllib.parse.urlsplit(BASE); sk = _socket.create_connection((u.hostname, u.port or 80), timeout=20)
sk.sendall((f"GET /sandboxes/{A}/ports/8090/chat HTTP/1.1\r\nHost: {u.netloc}\r\n" + (f"Authorization: {AUTH['Authorization']}\r\n" if AUTH else "") +
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n").encode())
hs = b""
try:
    while b"\r\n\r\n" not in hs: hs += sk.recv(4096)
except OSError as e: hs = repr(e).encode()
step("handshake answered 101 through the node", hs.startswith(b"HTTP/1.1 101"), hs.split(b"\r\n")[0][:60])
echoed = b""
if hs.startswith(b"HTTP/1.1 101"):
    sk.sendall(b"hello-tunnel")
    try: echoed = sk.recv(4096)
    except OSError as e: echoed = repr(e).encode()
step("bytes flow both ways after the upgrade", echoed == b"echo:hello-tunnel", echoed[:40])
sk.close()
sk = _socket.create_connection((u.hostname, u.port or 80), timeout=20)
sk.sendall((f"GET /sandboxes/{A}/ports/8080/index.html HTTP/1.1\r\nHost: {u.netloc}\r\n" + (f"Authorization: {AUTH['Authorization']}\r\n" if AUTH else "") + "Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n").encode())
hs = b""
try:
    while b"\r\n\r\n" not in hs: hs += sk.recv(4096)
except OSError as e: hs = repr(e).encode()
step("a server that declines the upgrade has its own answer passed through", hs.startswith(b"HTTP/1.") and not hs.startswith(b"HTTP/1.1 101"), hs.split(b"\r\n")[0][:60]); sk.close()
c, r = req("GET", f"/sandboxes/{A}/ports/99999/"); step("bad port is 400", c == 400)
c, r = req("GET", "/sandboxes/sb-nope/ports/8080/"); step("unknown sandbox is 404", c == 404)
c, p = req("POST", f"/sandboxes/{A}/pause"); c, body = req("GET", f"/sandboxes/{A}/ports/8080/index.html", raw=True)
step("a paused sandbox is resumed transparently for a request", c == 200 and b"served from" in body)
req("DELETE", f"/sandboxes/{A}"); step("deleted", True)
