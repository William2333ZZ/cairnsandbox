"""Credential injection for HTTPS-only APIs (D89): the sandbox sends plain HTTP, the proxy adds the credential and speaks
TLS to the server, verifying its certificate. A local TLS echo server stands in for the API; its certificate is issued
by a throwaway CA that only this test trusts (EGRESS_CA_FILE)."""
import json, os, shutil, socket, ssl, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import egress_proxy as ep

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs the openssl command to make a test CA")
IP = "10.200.0.40"


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "ca.ext"), "w").write("basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    open(os.path.join(d, "leaf.ext"), "w").write("basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                                 "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n"
                                                 "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
    _openssl("genrsa", "-out", "ca.key", "2048", cwd=d)
    _openssl("req", "-new", "-key", "ca.key", "-out", "ca.csr", "-subj", "/CN=egress test CA", cwd=d)
    _openssl("x509", "-req", "-in", "ca.csr", "-signkey", "ca.key", "-out", "ca.pem", "-days", "2", "-extfile", "ca.ext", cwd=d)
    _openssl("genrsa", "-out", "leaf.key", "2048", cwd=d)
    _openssl("req", "-new", "-key", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=localhost", cwd=d)
    _openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "leaf.pem", "-days", "2", "-extfile", "leaf.ext", cwd=d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def api(pki):
    """https://localhost:<port>/ echoing the path and the Authorization header it received."""
    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        do_POST = do_GET
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(os.path.join(pki, "leaf.pem"), os.path.join(pki, "leaf.key"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ep.POLICY.clear(); ep.CREDENTIALS.clear(); ep.RECENT.clear(); ep._TLS = None
    yield
    ep._TLS = None


def _through_proxy(request):
    """Send one raw request as the guest would and return (response bytes, audit records)."""
    guest, proxy_end = socket.socketpair()
    audit = []
    t = threading.Thread(target=ep.handle, args=(proxy_end, (IP, 40000), audit.append), daemon=True); t.start()
    guest.sendall(request)
    out = b""
    guest.settimeout(10)
    try:
        while True:
            d = guest.recv(65536)
            if not d: break
            out += d
    except socket.timeout:
        pass
    t.join(10)
    return out, audit


def _policy(port):
    ep.set_credential("ds", "Authorization", "Bearer sk-test-value")
    rules = ep.validate_rules([{"name": "ds", "match": {"scheme": "http", "host": "localhost", "path": "/v1/*"},
                                "action": {"allow": True, "inject": "ds", "upstream": "https"}}])
    ep.set_policy(IP, "sb-tls", [], mode="closed", rules=rules)


def test_the_key_reaches_the_https_server_and_never_the_logs(pki, api, monkeypatch):
    monkeypatch.setenv("EGRESS_CA_FILE", os.path.join(pki, "ca.pem"))
    _policy(api)
    resp, audit = _through_proxy(f"GET http://localhost:{api}/v1/models HTTP/1.1\r\nHost: localhost:{api}\r\nAccept: */*\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.0 200") or resp.startswith(b"HTTP/1.1 200"), resp[:120]
    body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
    assert body == {"path": "/v1/models", "auth": "Bearer sk-test-value"}
    rec = audit[0]
    assert rec["allowed"] and rec["rule"] == "ds" and rec["upstream"] == "https" and rec["injected"] == ["Authorization"]
    assert "sk-test-value" not in json.dumps(audit) and "sk-test-value" not in json.dumps(ep.recent("sb-tls"))


def test_an_untrusted_certificate_is_refused(api, monkeypatch):
    monkeypatch.delenv("EGRESS_CA_FILE", raising=False)          # only the system CAs: the test CA is unknown
    _policy(api)
    resp, audit = _through_proxy(f"GET http://localhost:{api}/v1/models HTTP/1.1\r\nHost: localhost:{api}\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.1 502"), resp[:80]
    err = [a for a in audit if a["op"] == "egress_error"]
    assert err and "CERTIFICATE_VERIFY_FAILED" in err[0]["error"] and "sk-test-value" not in json.dumps(audit)


def test_a_path_outside_the_rule_is_closed(api, pki, monkeypatch):
    monkeypatch.setenv("EGRESS_CA_FILE", os.path.join(pki, "ca.pem"))
    _policy(api)
    resp, audit = _through_proxy(f"GET http://localhost:{api}/admin HTTP/1.1\r\nHost: localhost:{api}\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.1 403") and audit[0]["allowed"] is False and "upstream" not in audit[0]


def test_rule_shape():
    ok = ep.validate_rules([{"name": "a", "match": {"scheme": "http", "host": "api.x"}, "action": {"allow": True, "upstream": "https"}}])
    assert ok[0]["action"] == {"allow": True, "upstream": "https"}
    for bad in ({"match": {"scheme": "http"}, "action": {"allow": True, "upstream": "tls"}},
                {"match": {"scheme": "http"}, "action": {"allow": False, "upstream": "https"}},
                {"match": {"host": "x"}, "action": {"allow": True, "upstream": "https"}},
                {"match": {"scheme": "https"}, "action": {"allow": True, "upstream": "https"}}):
        with pytest.raises(ValueError):
            ep.validate_rules([bad])
    # a CONNECT (https) request never gets an upstream, even if a rule would match it without a scheme
    ep.set_policy(IP, "sb", [], rules=ep.validate_rules([{"name": "h", "match": {"scheme": "http"}, "action": {"allow": True, "upstream": "https"}}]))
    assert ep.decide(IP, "https", "api.x", "CONNECT", "")[4] is None and ep.decide(IP, "http", "api.x", "GET", "/")[4] == "https"
