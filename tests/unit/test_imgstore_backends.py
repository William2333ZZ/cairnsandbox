"""fc-store 的两种后端(本地目录、S3 兼容桶)对外行为一致。S3 用进程内的假服务跑;设了 S3_ENDPOINT 等环境变量时再对真实的桶跑一遍。"""
import hashlib, json, os, sys, threading, time, urllib.parse, urllib.request, urllib.error, xml.sax.saxutils as X
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import imgstore  # noqa: E402


class FakeS3(BaseHTTPRequestHandler):
    objs = {}   # key -> (bytes, mtime)
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _key(self):
        u = urllib.parse.urlsplit(self.path); return urllib.parse.unquote(u.path).split("/", 2)[2] if u.path.count("/") >= 2 else "", urllib.parse.parse_qs(u.query)
    def _need_sig(self):
        a = self.headers.get("Authorization", "")
        if not a.startswith("AWS4-HMAC-SHA256 Credential=") or "Signature=" not in a or not self.headers.get("x-amz-date"):
            self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return False
        return True
    def _out(self, code, body=b"", extra=None):
        self.send_response(code); self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def do_PUT(self):
        if not self._need_sig(): return
        k, _ = self._key(); n = int(self.headers.get("Content-Length") or 0)
        FakeS3.objs[k] = (self.rfile.read(n), time.time()); self._out(200)
    def do_HEAD(self):
        if not self._need_sig(): return
        k, _ = self._key(); o = FakeS3.objs.get(k)
        self.send_response(200 if o else 404); self.send_header("Content-Length", str(len(o[0]) if o else 0)); self.end_headers()
    def do_DELETE(self):
        if not self._need_sig(): return
        k, _ = self._key(); self._out(204 if FakeS3.objs.pop(k, None) else 404)
    def do_GET(self):
        if not self._need_sig(): return
        k, q = self._key()
        if k == "" and "list-type" in q:
            pre = q.get("prefix", [""])[0]; keys = sorted(x for x in FakeS3.objs if x.startswith(pre))
            start = int(q.get("continuation-token", ["0"])[0]); page = keys[start:start + 2]   # tiny pages to exercise continuation
            more = start + 2 < len(keys)
            body = "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">" + "".join(
                f"<Contents><Key>{X.escape(x)}</Key><LastModified>{time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(FakeS3.objs[x][1]))}</LastModified><Size>{len(FakeS3.objs[x][0])}</Size></Contents>" for x in page
            ) + f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>" + (f"<NextContinuationToken>{start + 2}</NextContinuationToken>" if more else "") + "</ListBucketResult>"
            return self._out(200, body.encode())
        o = FakeS3.objs.get(k); self._out(200, o[0]) if o else self._out(404, b"<Error/>")


def _serve(backend):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    threading.Thread(target=imgstore.serve, args=("/unused", port, 0, backend), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try: urllib.request.urlopen(base + "/index.json", timeout=1); break
        except Exception: time.sleep(0.1)
    return base


def _call(method, url, data=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=10) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()


def scenario(base):
    chunk = os.urandom(5000); h = hashlib.sha256(chunk).hexdigest(); other = os.urandom(300); h2 = hashlib.sha256(other).hexdigest()
    assert _call("HEAD", f"{base}/chunks/{h}")[0] == 404
    assert _call("PUT", f"{base}/chunks/{'0' * 64}", chunk)[0] == 400                       # hash must match the body
    assert _call("PUT", f"{base}/chunks/{h}", chunk)[0] == 201 and _call("PUT", f"{base}/chunks/{h}", chunk)[0] == 201   # idempotent
    assert _call("PUT", f"{base}/chunks/{h2}", other)[0] == 201
    st, _ = _call("HEAD", f"{base}/chunks/{h}"); assert st == 200
    assert _call("GET", f"{base}/chunks/{h}") == (200, chunk)
    assert _call("GET", f"{base}/chunks/{'f' * 64}")[0] == 404
    for n in ("a", "b", "c"):                                                               # three images: forces list continuation in the fake
        assert _call("PUT", f"{base}/images/{n}.json", json.dumps({"chunks": [h] if n != "c" else [h2], "size": 1}).encode())[0] == 201
    assert _call("PUT", f"{base}/images/bad.json", b"{nope")[0] == 400
    assert sorted(json.loads(_call("GET", f"{base}/index.json")[1])["images"]) == ["a", "b", "c"]
    assert _call("PUT", f"{base}/bundles/bn.json", json.dumps({"images": {"disk": "a"}}).encode())[0] == 201
    assert json.loads(_call("GET", f"{base}/bundles.json")[1])["bundles"] == ["bn"]
    assert json.loads(_call("GET", f"{base}/bundles/bn.json")[1])["images"] == {"disk": "a"}
    assert _call("DELETE", f"{base}/bundles/bn.json")[0] == 200                              # drops its image too
    assert _call("GET", f"{base}/images/a.json")[0] == 404 and _call("DELETE", f"{base}/bundles/bn.json")[0] == 404
    assert _call("DELETE", f"{base}/images/c.json")[0] == 200                               # h2 is now unreferenced
    r = json.loads(_call("POST", f"{base}/gc", b"")[1]); assert r["kept"] == 1               # fresh chunks survive the 60 s grace
    assert _call("GET", f"{base}/chunks/{h2}")[0] == 200


def test_local_directory_backend(tmp_path):
    scenario(_serve(imgstore.FsBackend(str(tmp_path))))


def test_s3_backend_against_a_fake_bucket():
    FakeS3.objs.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3); threading.Thread(target=srv.serve_forever, daemon=True).start()
    scenario(_serve(imgstore.S3Backend(f"http://127.0.0.1:{srv.server_port}", "b", "AK", "SK")))
    assert any(k.startswith("chunks/") for k in FakeS3.objs)


@pytest.mark.skipif(not os.environ.get("S3_ENDPOINT"), reason="no real S3 endpoint configured")
def test_s3_backend_against_a_real_bucket():
    be = imgstore.s3_from_env(); be.bucket = os.environ.get("S3_TEST_BUCKET", be.bucket)
    scenario(_serve(be))


def test_migrate_copies_a_local_store_into_a_bucket(tmp_path):
    FakeS3.objs.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3); threading.Thread(target=srv.serve_forever, daemon=True).start()
    fs = imgstore.FsBackend(str(tmp_path)); c = os.urandom(900); h = hashlib.sha256(c).hexdigest()
    fs.put(f"chunks/{h}", c); fs.put("images/x.json", json.dumps({"chunks": [h]}).encode()); fs.put("bundles/b.json", b'{"images":{}}')
    dst = imgstore.S3Backend(f"http://127.0.0.1:{srv.server_port}", "b", "AK", "SK")
    imgstore.migrate(str(tmp_path), dst); imgstore.migrate(str(tmp_path), dst)      # second run is a no-op
    assert dst.get(f"chunks/{h}") == c and dst.get("images/x.json") and dst.get("bundles/b.json")
    assert len(FakeS3.objs) == 3


def test_the_zero_chunk_hash_helper_exists_and_is_stable():
    h = imgstore._zero_hash(1 << 20)
    assert h == hashlib.sha256(bytes(1 << 20)).hexdigest() and imgstore._zero_hash(1 << 20) == h


@pytest.mark.skipif(sys.platform != "linux", reason="push_remote walks the file's data extents with SEEK_DATA (Linux)")
def test_a_file_with_zero_chunks_is_pushed_and_pulled_through_either_backend(tmp_path):
    """The node publishes sparse disk images through push_remote (zero chunks collapse to one): a regression test for a helper table lost in a refactor (D52)."""
    src = tmp_path / "disk.img"; data = bytearray(3 << 20); data[100:110] = b"hello-disk"; data[(2 << 20) + 5:(2 << 20) + 9] = b"tail"; src.write_bytes(bytes(data))
    for backend in (imgstore.FsBackend(str(tmp_path / "store")), None):
        if backend is None:
            FakeS3.objs.clear(); srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3); threading.Thread(target=srv.serve_forever, daemon=True).start()
            backend = imgstore.S3Backend(f"http://127.0.0.1:{srv.server_port}", "b", "AK", "SK")
        base = _serve(backend)
        imgstore.push_remote(base, "disk", str(src))
        out = tmp_path / "out.img"; imgstore.pull_remote(base, "disk", str(out))
        assert out.read_bytes() == bytes(data)
