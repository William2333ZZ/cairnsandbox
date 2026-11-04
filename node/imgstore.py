#!/usr/bin/env python3
"""Content-addressed chunk store for images. Backends: local directory (default) or any S3-compatible bucket (set S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY).

  imgstore.py push  <store_dir> <name> <file> [--chunk-mib 1]
  imgstore.py serve <store_dir> <port> [--bw-mbps N]      # HTTP: /index.json, /images/<name>.json, /chunks/<hash>
  imgstore.py migrate <store_dir>                          # copy a local store into the S3 bucket from S3_* env (idempotent)
  imgstore.py profile <store_dir> <name> <indices.json>   # attach a boot profile (chunk indices to prefetch)
  imgstore.py pull <store_url> <name> <out_file>          # eager baseline: download every chunk, rebuild the file

Layout: <store>/chunks/<sha256>  (dedup'd, zero chunks collapse to one)  <store>/images/<name>.json
HTTP write side (used by nodes to publish paused sandboxes and template snapshots):
  HEAD /chunks/<hash>  PUT /chunks/<hash>  PUT /images/<name>.json  PUT|GET /bundles/<name>.json  GET /bundles.json
  DELETE /bundles/<name>.json (and the images it lists)  DELETE /images/<name>.json  POST /gc (drop unreferenced chunks)
"""
import datetime, hashlib, hmac, json, os, sys, threading, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

SEEK_DATA, SEEK_HOLE = 3, 4
def data_extents(path):
    """Allocated ranges of a sparse file. A Diff memory layer's holes mean "page unchanged", so they must travel
    with the file: chunking at 1 MiB would otherwise turn them into zero pages."""
    out = []; size = os.path.getsize(path); fd = os.open(path, os.O_RDONLY)
    try:
        pos = 0
        while pos < size:
            try: start = os.lseek(fd, pos, SEEK_DATA)
            except OSError: break
            end = os.lseek(fd, start, SEEK_HOLE); out.append([start, end - start]); pos = end
    finally: os.close(fd)
    return out
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def push(store, name, path, chunk_mib=1):
    cs = chunk_mib << 20; os.makedirs(f"{store}/chunks", exist_ok=True); os.makedirs(f"{store}/images", exist_ok=True)
    hashes = []; unique = 0; t0 = time.time()
    with open(path, "rb") as f:
        while True:
            b = f.read(cs)
            if not b: break
            h = hashlib.sha256(b).hexdigest(); hashes.append(h)
            p = f"{store}/chunks/{h}"
            if not os.path.exists(p):
                with open(p + ".tmp", "wb") as o: o.write(b)
                os.rename(p + ".tmp", p); unique += 1
    m = {"name": name, "size": os.path.getsize(path), "chunk_size": cs, "chunks": hashes, "profile": []}
    json.dump(m, open(f"{store}/images/{name}.json", "w"))
    print(f"pushed {name}: {len(hashes)} chunks, {unique} new, {len(set(hashes))} unique, {time.time()-t0:.1f}s")


def set_profile(store, name, indices):
    p = f"{store}/images/{name}.json"; m = json.load(open(p)); m["profile"] = sorted(set(indices)); json.dump(m, open(p, "w"))
    print(f"profile for {name}: {len(m['profile'])} chunks")


class FsBackend:
    """本地目录:chunks/<sha256>、images/<name>.json、bundles/<name>.json。"""
    def __init__(self, root): self.root = root
    def head(self, key):
        p = f"{self.root}/{key}"; return os.path.getsize(p) if os.path.isfile(p) else None
    def get(self, key):
        p = f"{self.root}/{key}"
        if not os.path.isfile(p): return None
        with open(p, "rb") as f: return f.read()
    def put(self, key, data):
        p = f"{self.root}/{key}"; os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "wb") as o: o.write(data)
        os.rename(tmp, p)
    def delete(self, key):
        try: os.unlink(f"{self.root}/{key}"); return True
        except FileNotFoundError: return False
    def list(self, prefix):
        d = f"{self.root}/{prefix}".rstrip("/"); out = []
        for f in os.listdir(d) if os.path.isdir(d) else []:
            if ".tmp." in f: continue
            st = os.stat(f"{d}/{f}"); out.append((f, st.st_size, st.st_mtime))
        return out


class S3Backend:
    """S3 兼容对象存储(Garage / MinIO / AWS):同样的键布局,放在一个桶里。只用标准库 + SigV4,路径风格寻址。"""
    def __init__(self, endpoint, bucket, access_key, secret_key, region="garage"):
        u = urllib.parse.urlsplit(endpoint); self.scheme, self.host = u.scheme, u.netloc
        self.bucket, self.ak, self.sk, self.region = bucket, access_key, secret_key, region
    def _req(self, method, key="", query=None, data=b"", ok=(200,)):
        path = "/" + self.bucket + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")
        qs = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}" for k, v in sorted((query or {}).items()))
        now = datetime.datetime.now(datetime.timezone.utc); amz = now.strftime("%Y%m%dT%H%M%SZ"); day = amz[:8]
        ph = hashlib.sha256(data).hexdigest()
        hdrs = {"host": self.host, "x-amz-content-sha256": ph, "x-amz-date": amz}
        signed = ";".join(sorted(hdrs)); canon = "\n".join([method, path, qs, "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs)), signed, ph])
        scope = f"{day}/{self.region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
        k = ("AWS4" + self.sk).encode()
        for m in (day, self.region, "s3", "aws4_request"): k = hmac.new(k, m.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        hdrs["Authorization"] = f"AWS4-HMAC-SHA256 Credential={self.ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
        req = urllib.request.Request(f"{self.scheme}://{self.host}{path}" + (f"?{qs}" if qs else ""), data=data if method == "PUT" else None, method=method, headers={k.title() if k != "host" else "Host": v for k, v in hdrs.items()})
        try:
            with urllib.request.urlopen(req, timeout=60) as r: return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            if e.code == 404 or e.code in ok: return e.code, e.read(), e.headers
            raise
    def head(self, key):
        st, _, h = self._req("HEAD", key); return int(h["Content-Length"]) if st == 200 else None
    def get(self, key):
        st, body, _ = self._req("GET", key); return body if st == 200 else None
    def put(self, key, data): self._req("PUT", key, data=data)
    def delete(self, key):
        st, _, _ = self._req("DELETE", key, ok=(200, 204)); return st != 404
    def list(self, prefix):
        out, token = [], None
        while True:
            q = {"list-type": 2, "prefix": prefix.rstrip("/") + "/"}
            if token: q["continuation-token"] = token
            _, body, _ = self._req("GET", "", query=q)
            root = ET.fromstring(body); ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            for c in root.findall(f"{ns}Contents"):
                k = c.find(f"{ns}Key").text; lm = c.find(f"{ns}LastModified").text
                out.append((k[len(prefix.rstrip("/")) + 1:], int(c.find(f"{ns}Size").text), datetime.datetime.fromisoformat(lm.replace("Z", "+00:00")).timestamp()))
            if root.findtext(f"{ns}IsTruncated") == "true": token = root.findtext(f"{ns}NextContinuationToken")
            else: return out


def serve(store, port, bw_mbps=0, backend=None):
    be = backend or FsBackend(store)
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _body(self):
            n = int(self.headers.get("Content-Length") or 0); return self.rfile.read(n) if n else b""
        def _fail(self, e):
            print(f"imgstore backend error: {self.command} {self.path}: {e}", flush=True); self._send(502, b"backend error")
        def do_HEAD(self):
            try: n = be.head(self.path[1:]) if self.path.startswith("/chunks/") else None
            except Exception as e: n = None; print(f"imgstore backend error: {e}", flush=True)
            if n is not None: self.send_response(200); self.send_header("Content-Length", str(n)); self.end_headers()
            else: self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
        def do_PUT(self):
            data = self._body()
            try:
                if self.path.startswith("/chunks/"):
                    h = self.path[len("/chunks/"):]
                    if "/" in h or hashlib.sha256(data).hexdigest() != h: return self._send(400, b"hash mismatch")
                    if be.head(f"chunks/{h}") is None: be.put(f"chunks/{h}", data)
                    return self._send(201, b"ok")
                for kind in ("images", "bundles"):
                    if self.path.startswith(f"/{kind}/") and self.path.endswith(".json") and "/" not in self.path[len(kind) + 2:]:
                        try: json.loads(data)
                        except ValueError: return self._send(400, b"bad json")
                        be.put(self.path[1:], data); return self._send(201, b"ok")
            except Exception as e: return self._fail(e)
            self._send(404, b"no route")
        def do_DELETE(self):
            key = self.path[1:]
            try:
                if self.path.startswith("/bundles/") and self.path.endswith(".json"):
                    raw = be.get(key)
                    if raw is None: return self._send(404, b"not found")
                    try:
                        for img in json.loads(raw).get("images", {}).values(): be.delete(f"images/{img}.json")
                    except ValueError: pass
                    be.delete(key); return self._send(200, b"ok")
                if self.path.startswith("/images/") and self.path.endswith(".json") and be.delete(key): return self._send(200, b"ok")
            except Exception as e: return self._fail(e)
            self._send(404, b"not found")
        def do_POST(self):
            if self.path == "/gc":
                self._body()
                try:
                    used = set()
                    for name, _, _ in be.list("images"):
                        try: used.update(json.loads(be.get(f"images/{name}"))["chunks"])
                        except (ValueError, KeyError, TypeError): pass
                    removed = freed = 0
                    for h, size, mtime in be.list("chunks"):
                        if h not in used and time.time() - mtime > 60:   # grace for in-flight pushes
                            freed += size; be.delete(f"chunks/{h}"); removed += 1
                except Exception as e: return self._fail(e)
                return self._send(200, json.dumps({"removed": removed, "freed_mib": round(freed / 1048576, 1), "kept": len(used)}).encode())
            self._send(404, b"no route")
        def do_GET(self):
            try:
                if self.path == "/bundles.json":
                    return self._send(200, json.dumps({"bundles": sorted(n[:-5] for n, _, _ in be.list("bundles") if n.endswith(".json"))}).encode())
                if self.path == "/index.json":
                    return self._send(200, json.dumps({"images": [n[:-5] for n, _, _ in be.list("images") if n.endswith(".json")]}).encode())
                if self.path.startswith(("/bundles/", "/images/", "/chunks/")):
                    data = be.get(self.path[1:])
                    if data is None: return self._send(404, b"not found")
                    return self._send(200, data, throttle=self.path.startswith("/chunks/"))
            except Exception as e: return self._fail(e)
            self._send(404, b"no route")
        def _send(self, code, data, throttle=False):
            self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers()
            if throttle and bw_mbps:          # emulate a remote link: pace the body at bw_mbps
                step = 256 << 10
                for i in range(0, len(data), step):
                    self.wfile.write(data[i:i + step]); self.wfile.flush(); time.sleep(step * 8 / (bw_mbps * 1e6))
            else:
                self.wfile.write(data)
    print(f"imgstore serving {'s3 ' + be.bucket if isinstance(be, S3Backend) else store} on :{port} bw={bw_mbps or 'unlimited'} Mbps", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def migrate(src_dir, dst, workers=8):
    """把本地目录里的 chunks/images/bundles 复制进另一个后端(幂等:块按哈希,已有的跳过)。先拷块、最后拷清单,中途断了也不会出现指向缺块的清单。"""
    from concurrent.futures import ThreadPoolExecutor
    src = FsBackend(src_dir); done = skipped = 0
    def one(h):
        if dst.head(f"chunks/{h}") is not None: return 0
        data = src.get(f"chunks/{h}")
        if hashlib.sha256(data).hexdigest() != h: raise ValueError(f"source chunk {h} is corrupt")
        dst.put(f"chunks/{h}", data); return 1
    names = [n for n, _, _ in src.list("chunks")]
    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(one, names): done += r; skipped += 1 - r
    docs = 0
    for kind in ("images", "bundles"):
        for n, _, _ in src.list(kind):
            if n.endswith(".json"): dst.put(f"{kind}/{n}", src.get(f"{kind}/{n}")); docs += 1
    print(f"migrated chunks={done} already_there={skipped} docs={docs}", flush=True)


def s3_from_env():
    """S3_ENDPOINT / S3_BUCKET / S3_ACCESS_KEY / S3_SECRET_KEY 都在才用 S3;密钥只从环境读,不上命令行。"""
    e = os.environ
    if not e.get("S3_ENDPOINT"): return None
    return S3Backend(e["S3_ENDPOINT"], e.get("S3_BUCKET", "fc-store"), e["S3_ACCESS_KEY"], e["S3_SECRET_KEY"], e.get("S3_REGION", "garage"))


ZERO_HASHES = {}


def _zero_hash(cs):
    if cs not in ZERO_HASHES: ZERO_HASHES[cs] = hashlib.sha256(bytes(cs)).hexdigest()
    return ZERO_HASHES[cs]


def _http(method, url, data=None, timeout=300):
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()


def push_remote(url, name, path, chunk_mib=1):
    """Chunk a local file and upload only the chunks the store does not have. Returns the manifest and stats."""
    cs = chunk_mib << 20; hashes = []; sent = 0; t0 = time.time(); zh = _zero_hash(cs)
    with open(path, "rb") as f:
        while True:
            b = f.read(cs)
            if not b: break
            if len(b) < cs: b = b + bytes(cs - len(b))
            h = hashlib.sha256(b).hexdigest(); hashes.append(h)
            if h == zh: continue                                      # the store always answers a zero chunk
            if _http("HEAD", f"{url}/chunks/{h}")[0] != 200:
                _http("PUT", f"{url}/chunks/{h}", b); sent += 1
    if zh in hashes and _http("HEAD", f"{url}/chunks/{zh}")[0] != 200: _http("PUT", f"{url}/chunks/{zh}", bytes(cs))
    m = {"name": name, "size": os.path.getsize(path), "chunk_size": cs, "chunks": hashes, "profile": [], "extents": data_extents(path)}
    _http("PUT", f"{url}/images/{name}.json", json.dumps(m).encode())
    return m, {"chunks": len(hashes), "sent": sent, "sent_mib": round(sent * cs / 1048576, 1), "s": round(time.time() - t0, 3)}


def pull_remote(url, name, out):
    """Rebuild a file from the store; zero chunks become holes so sparse memory layers stay sparse."""
    st, body = _http("GET", f"{url}/images/{name}.json")
    if st != 200: raise FileNotFoundError(f"no image {name} in store")
    m = json.loads(body); cs = m["chunk_size"]; zh = _zero_hash(cs); got = 0; t0 = time.time(); ext = m.get("extents")
    with open(out, "wb") as f:
        f.truncate(m["size"])
        for i, h in enumerate(m["chunks"]):
            if h == zh: continue
            st, data = _http("GET", f"{url}/chunks/{h}")
            if st != 200: raise IOError(f"chunk {h} missing")
            got += 1; c0, c1 = i * cs, min((i + 1) * cs, m["size"])
            if ext is None: f.seek(c0); f.write(data[:c1 - c0]); continue
            for e0, ln in ext:                                      # write only what was allocated in the source
                a, b = max(c0, e0), min(c1, e0 + ln)
                if a < b: f.seek(a); f.write(data[a - c0:b - c0])
    return {"chunks": len(m["chunks"]), "fetched": got, "fetched_mib": round(got * cs / 1048576, 1), "s": round(time.time() - t0, 3)}


def put_bundle(url, name, bundle): return _http("PUT", f"{url}/bundles/{name}.json", json.dumps(bundle).encode())[0]
def get_bundle(url, name):
    st, body = _http("GET", f"{url}/bundles/{name}.json")
    return json.loads(body) if st == 200 else None
def delete_bundle(url, name): return _http("DELETE", f"{url}/bundles/{name}.json")[0]
def gc(url):
    st, body = _http("POST", f"{url}/gc", b""); return json.loads(body) if st == 200 else {"error": st}
def list_bundles(url):
    st, body = _http("GET", f"{url}/bundles.json"); return json.loads(body)["bundles"] if st == 200 else []


def pull(url, name, out):
    import urllib.request
    m = json.loads(urllib.request.urlopen(f"{url}/images/{name}.json").read()); t0 = time.time(); n = 0
    with open(out, "wb") as f:
        for h in m["chunks"]:
            f.write(urllib.request.urlopen(f"{url}/chunks/{h}").read()); n += 1
    print(json.dumps({"name": name, "chunks": n, "bytes": m["size"], "s": round(time.time() - t0, 2)}))


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "push": push(a[1], a[2], a[3], int(a[5]) if len(a) > 5 and a[4] == "--chunk-mib" else 1)
    elif a[0] == "serve": serve(a[1], int(a[2]), int(a[4]) if len(a) > 4 and a[3] == "--bw-mbps" else 0, s3_from_env())
    elif a[0] == "migrate": migrate(a[1], s3_from_env() or sys.exit("set S3_* env"))
    elif a[0] == "profile": set_profile(a[1], a[2], json.load(open(a[3])))
    elif a[0] == "pull": pull(a[1], a[2], a[3])
    else: print(__doc__)
