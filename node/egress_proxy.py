#!/usr/bin/env python3
"""Per-node egress proxy: HTTP CONNECT + plain HTTP forwarding, policy by client (guest) IP (docs/002 沙箱侧 6 网络与出口; D36).

policy per guest ip: {"sandbox", "mode": closed|allowlist|audited, "allow_hosts": [...], "rules": [...]}
  rules (first match wins): {"name", "match": {"scheme"?: http|https, "host"?: "exact" | "*.suffix" | glob | "~regex", "method"?: [..], "path"?: "/exact" | "/prefix/*"},
                             "action": {"allow": bool, "inject"?: "credential name", "upstream"?: "https"}}
  method and path only apply to plain HTTP (a CONNECT tunnel shows the proxy nothing but host and port); inject adds the credential's header to a
  plain-HTTP request before it goes upstream — the value lives on the node and never enters the sandbox.
  upstream "https" (D89): the sandbox sends plain http://host/... and the proxy opens TLS to the server itself (port 443 unless the URL names
  one), verifying its certificate against the system CAs (plus EGRESS_CA_FILE if set). That is how inject works with HTTPS-only APIs.
  No rule matched: audited -> allow; else allow_hosts (glob; empty = deny all).
  limits (per sandbox, 0 = none): {"requests_per_min", "max_bytes"}: past either, the proxy answers 429 (egress_limited) until the window moves / the policy is reset.
Every decision is reported through `audit_cb(dict)` and kept in a per-sandbox ring (recent()).
"""
import collections, fnmatch, json, os, re, select, socket, ssl, threading, time, urllib.parse

POLICY = {}          # guest ip -> {"sandbox", "mode", "allow_hosts", "rules", "limits"}
_REQS = {}           # guest ip -> deque of request timestamps (requests_per_min)
_plock = threading.Lock()
MAX_CONN_PER_SANDBOX = 32
_inflight = {}       # guest ip -> open tunnel count
CREDENTIALS = {}     # name -> {"header", "value"}; the node's own vault, loaded from a file and settable by the deployer
RECENT = {}          # sandbox -> deque of the last decisions
RECENT_N = 200
BYTES = {}           # guest ip -> {"out": bytes the guest sent, "in": bytes it received}; cumulative while the policy exists (metering reads the deltas)
MODES = ("closed", "allowlist", "audited")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


def set_policy(ip, sandbox, allow_hosts, mode=None, rules=(), limits=None):
    if mode is None: mode = "allowlist" if allow_hosts else "closed"
    with _plock: POLICY[ip] = {"sandbox": sandbox, "mode": mode, "allow_hosts": list(allow_hosts), "rules": list(rules or []), "limits": dict(limits or {})}

def clear_policy(ip):
    with _plock: POLICY.pop(ip, None); BYTES.pop(ip, None); _REQS.pop(ip, None)


def validate_limits(limits):
    """{"requests_per_min", "max_bytes"}: non-negative integers, 0 = no limit; unknown keys are an error."""
    out = {"requests_per_min": 0, "max_bytes": 0}
    for k, v in (limits or {}).items():
        if k not in out: raise ValueError(f"limits: unknown key {k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or int(v) != v: raise ValueError(f"limits.{k}: a non-negative integer")
        out[k] = int(v)
    return out

def limited(ip, t=None):
    """None, or why this request must be refused: the sandbox is past its request rate or its byte budget. Counts the request when it passes."""
    p = POLICY.get(ip)
    if not p: return None
    lim = p.get("limits") or {}; t = t if t is not None else time.time()
    with _plock:
        if lim.get("max_bytes"):
            b = BYTES.get(ip) or {}
            if int(b.get("out", 0)) + int(b.get("in", 0)) >= lim["max_bytes"]: return f"byte budget {lim['max_bytes']} used"
        if lim.get("requests_per_min"):
            q = _REQS.setdefault(ip, collections.deque())
            while q and q[0] <= t - 60: q.popleft()
            if len(q) >= lim["requests_per_min"]: return f"{lim['requests_per_min']} requests per minute"
            q.append(t)
    return None

def traffic(ip):
    """{"out", "in"} bytes through the proxy for this guest ip (0 when none)."""
    b = BYTES.get(ip) or {}
    return {"out": int(b.get("out", 0)), "in": int(b.get("in", 0))}

def _count(ip, direction, n):
    with _plock:
        b = BYTES.setdefault(ip, {"out": 0, "in": 0}); b[direction] += n


def validate_rules(rules):
    """Shape-check a rule list; raises ValueError with the first problem. Returns the normalized list."""
    out = []
    for i, r in enumerate(rules or []):
        if not isinstance(r, dict): raise ValueError(f"rule {i}: must be an object")
        name = str(r.get("name") or f"rule{i}")
        if not NAME_RE.match(name): raise ValueError(f"rule {i}: bad name {name!r}")
        m = r.get("match") or {}; a = r.get("action") or {}
        if not isinstance(m, dict) or not isinstance(a, dict): raise ValueError(f"rule {name}: match and action must be objects")
        if m.get("scheme") not in (None, "http", "https"): raise ValueError(f"rule {name}: scheme must be http or https")
        if "method" in m and (not isinstance(m["method"], list) or not all(isinstance(x, str) for x in m["method"])): raise ValueError(f"rule {name}: method must be a list")
        if "path" in m and (not isinstance(m["path"], str) or not m["path"].startswith("/")): raise ValueError(f"rule {name}: path must start with /")
        if "host" in m and not isinstance(m["host"], str): raise ValueError(f"rule {name}: host must be a string")
        if isinstance(m.get("host"), str) and m["host"].startswith("~"):
            try: re.compile(m["host"][1:])
            except re.error as e: raise ValueError(f"rule {name}: bad host regex: {e}")
        if "allow" not in a: raise ValueError(f"rule {name}: action.allow is required")
        if a.get("inject") is not None and not NAME_RE.match(str(a["inject"])): raise ValueError(f"rule {name}: bad credential name")
        if a.get("inject") and not a.get("allow"): raise ValueError(f"rule {name}: inject on a deny rule")
        if a.get("upstream") is not None:
            if a["upstream"] != "https": raise ValueError(f"rule {name}: upstream must be \"https\"")
            if not a.get("allow"): raise ValueError(f"rule {name}: upstream on a deny rule")
            if m.get("scheme") != "http": raise ValueError(f"rule {name}: upstream needs match.scheme \"http\" (the request the sandbox sends)")
        out.append({"name": name, "match": {k: m[k] for k in ("scheme", "host", "method", "path") if k in m},
                    "action": {"allow": bool(a["allow"]), **({"inject": a["inject"]} if a.get("inject") else {}), **({"upstream": "https"} if a.get("upstream") else {})}})
    return out


def _host_match(pat, host):
    if pat.startswith("~"): return re.fullmatch(pat[1:], host, re.I) is not None     # ~regex: the whole host name
    pat, host = pat.lower(), host.lower()
    if pat.startswith("*."): return host.endswith(pat[1:]) and host != pat[2:]      # *.example.com: any subdomain, not the apex
    if "*" in pat or "?" in pat: return fnmatch.fnmatch(host, pat)
    return host == pat

def _path_match(pat, path):
    if pat.endswith("*"): return path.startswith(pat[:-1])
    return path == pat


def decide(ip, scheme, host, method, path):
    """(allowed, sandbox, rule name or None, inject headers, upstream scheme or None)."""
    p = POLICY.get(ip)
    if not p: return False, None, None, {}, None
    for r in p["rules"]:
        m = r["match"]
        if m.get("scheme") and m["scheme"] != scheme: continue
        if m.get("host") and not _host_match(m["host"], host): continue
        if m.get("method") and (scheme != "http" or method.upper() not in [x.upper() for x in m["method"]]): continue
        if m.get("path") and (scheme != "http" or not _path_match(m["path"], path)): continue
        inject = {}
        cred = r["action"].get("inject")
        if cred and cred in CREDENTIALS and scheme == "http": inject = {CREDENTIALS[cred]["header"]: CREDENTIALS[cred]["value"]}
        up = r["action"].get("upstream") if scheme == "http" else None
        return bool(r["action"]["allow"]), p["sandbox"], r["name"], inject, up
    if p["mode"] == "audited": return True, p["sandbox"], None, {}, None
    return any(fnmatch.fnmatch(host.lower(), pat.lower()) for pat in p["allow_hosts"]), p["sandbox"], None, {}, None


def allowed(ip, host):
    ok, sb, *_ = decide(ip, "https", host, "CONNECT", "")
    return ok, sb


_TLS = None
def tls_context():
    """Certificates are always verified: the system CAs, plus EGRESS_CA_FILE when a deployment has a private CA. There is no switch to turn it off."""
    global _TLS
    if _TLS is None:
        ctx = ssl.create_default_context()
        extra = os.environ.get("EGRESS_CA_FILE")
        if extra: ctx.load_verify_locations(cafile=extra)
        _TLS = ctx
    return _TLS


def recent(sandbox): return list(RECENT.get(sandbox, ()))

def _remember(sb, rec):
    if not sb: return
    with _plock: RECENT.setdefault(sb, collections.deque(maxlen=RECENT_N)).append(rec)

def forget(sandbox):
    with _plock: RECENT.pop(sandbox, None)


# ---- credentials: the node's vault; names are public, values never leave this process ----
def load_credentials(path):
    try: doc = json.load(open(path))
    except (OSError, ValueError): return 0
    n = 0
    for name, c in (doc or {}).items():
        if NAME_RE.match(str(name)) and isinstance(c, dict) and c.get("header") and c.get("value"):
            CREDENTIALS[name] = {"header": str(c["header"]), "value": str(c["value"])}; n += 1
    return n

def set_credential(name, header, value, path=None):
    if not NAME_RE.match(str(name)): raise ValueError("bad credential name")
    if not header or not re.match(r"^[A-Za-z0-9-]+$", str(header)): raise ValueError("bad header name")
    if not value: raise ValueError("value required")
    CREDENTIALS[name] = {"header": str(header), "value": str(value)}
    if path: _persist(path)

def drop_credential(name, path=None):
    CREDENTIALS.pop(name, None)
    if path: _persist(path)

def _persist(path):
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: json.dump(CREDENTIALS, f)
    os.replace(tmp, path)

def credential_names(): return sorted(CREDENTIALS)


def _pipe(a, b, ip=None, idle=300):
    """a = the guest's socket, b = upstream; bytes are counted per direction for metering. Closes both after `idle` seconds without traffic."""
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], idle)
            if not r: break
            for s in r:
                d = s.recv(65536)
                if not d: return
                while isinstance(s, ssl.SSLSocket) and s.pending(): d += s.recv(s.pending())     # decrypted bytes select() cannot see
                (b if s is a else a).sendall(d)
                if ip: _count(ip, "out" if s is a else "in", len(d))
    except OSError:
        pass
    finally:
        for s in (a, b):
            try: s.close()
            except OSError: pass


def handle(client, addr, audit_cb):
    ip = addr[0]; counted = False
    try:
        f = client.makefile("rb"); line = f.readline()
        if not line: client.close(); return
        method, target, ver = line.decode(errors="replace").split()[:3]
        headers = {}
        while True:
            h = f.readline()
            if h in (b"\r\n", b"\n", b""): break
            k, _, v = h.decode(errors="replace").partition(":"); headers[k.strip().lower()] = v.strip()
        if method == "CONNECT":
            host, _, port = target.rpartition(":"); port = int(port or 443); scheme, path = "https", ""
        else:
            u = urllib.parse.urlsplit(target); host, port = u.hostname, u.port or 80; scheme, path = "http", u.path or "/"
        ok, sb, rule, inject, upstream = decide(ip, scheme, host or "", method, path)
        if upstream == "https" and not u.port: port = 443                             # http://api.example.com/ goes to the server's TLS port
        rec = {"op": "egress", "sandbox": sb, "guest_ip": ip, "method": method, "scheme": scheme, "host": host, "port": port, "path": path if scheme == "http" else None,
               "allowed": ok, "rule": rule, "injected": sorted(k for k in inject), **({"upstream": upstream} if upstream else {})}
        audit_cb(rec); _remember(sb, {k: v for k, v in rec.items() if k not in ("op", "guest_ip")})
        if not ok:
            msg = deny_message(sb, host)       # the reason phrase is what Go and curl show the caller, so it says what to do, not just "Forbidden"
            client.sendall(f"HTTP/1.1 403 {msg}\r\nContent-Type: text/plain\r\nContent-Length: {len(msg) + 2}\r\nConnection: close\r\n\r\n{msg}\r\n".encode())
            client.close(); return
        why = limited(ip)
        if why:
            client.sendall(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"); client.close()
            audit_cb({"op": "egress_limited", "sandbox": sb, "guest_ip": ip, "host": host, "reason": why}); _remember(sb, {"host": host, "allowed": False, "limited": why}); return
        with _plock:
            if _inflight.get(ip, 0) >= MAX_CONN_PER_SANDBOX:
                client.sendall(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"); client.close()
                audit_cb({"op": "egress_limited", "sandbox": sb, "guest_ip": ip}); return
            _inflight[ip] = _inflight.get(ip, 0) + 1
        counted = True
        up = socket.create_connection((host, port), timeout=15)
        if upstream == "https": up = tls_context().wrap_socket(up, server_hostname=host)   # verified; a bad certificate raises and the guest gets 502
        if method == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _pipe(client, up, ip)
        else:
            path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, "")); headers.pop("proxy-connection", None); headers["connection"] = "close"
            for k, v in inject.items(): headers[k.lower()] = v                         # the credential the sandbox never saw
            req = f"{method} {path} {ver}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            up.sendall(req.encode()); _count(ip, "out", len(req))
            n = int(headers.get("content-length") or 0)
            if n: body = f.read(n); up.sendall(body); _count(ip, "out", len(body))
            _pipe(client, up, ip)
    except Exception as e:  # noqa: BLE001
        try: client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError: pass
        audit_cb({"op": "egress_error", "guest_ip": ip, "error": repr(e)})
        try: client.close()
        except OSError: pass
    finally:
        if counted:
            with _plock: _inflight[ip] = max(0, _inflight.get(ip, 1) - 1)


def deny_message(sandbox, host):
    """One line, ASCII, no CR/LF (it goes into the status line): which host, whose policy, and how to ask."""
    h = "".join(c for c in (host or "?") if c.isalnum() or c in ".-_:[]")[:100]; sid = "".join(c for c in (sandbox or "") if c.isalnum() or c in "-_")[:40]
    return f"egress denied for {h} by the sandbox network policy; to allow it: PUT /sandboxes/{sid or '<id>'}/network " + '{"allow_hosts":["' + h + '"]}'


def serve(port, audit_cb):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(128)
    while True:
        c, a = s.accept(); threading.Thread(target=handle, args=(c, a, audit_cb), daemon=True).start()


def start(port, audit_cb):
    threading.Thread(target=serve, args=(port, audit_cb), daemon=True).start()
