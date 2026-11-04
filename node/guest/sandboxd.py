#!/usr/bin/env python3
"""Guest-side sandbox daemon (v1). AF_VSOCK listener, newline-delimited JSON.

Hardening: only accepts connections from the host CID; task processes run as the unprivileged
`agent` user by default; the daemon and its private shell live in the read-only base layer;
per-stream output is capped. Sessions: persistent shells (pipe or PTY) with streaming output.

request : {"id", "op", ...}      response: {"id", "ok", ...}
streamed: {"id", "event": "out", "data"}  (only while an exec/session_exec call is in flight)
"""
import base64, ctypes, json, os, pty, pwd, queue, re, select, signal, socket, subprocess, sys, threading, time, uuid

PORT = int(os.environ.get("SANDBOXD_PORT", "5000"))
HOST_CID = 2
SHELL = "/usr/local/lib/sandboxd/bash"      # private copy in the read-only layer
DEFAULT_USER = "agent"
MAX_OUT = 1 << 20                           # 1 MiB per stream / per exec
BOOT_T = time.monotonic()


def _user(name):
    p = pwd.getpwnam(name)
    return p.pw_uid, p.pw_gid, p.pw_dir


NET_FILE = "/run/sandboxd/net.json"

def _net_env():
    try:
        n = json.load(open(NET_FILE)); px = n.get("proxy")
        return {"http_proxy": px, "https_proxy": px, "HTTP_PROXY": px, "HTTPS_PROXY": px, "no_proxy": "localhost,127.0.0.1"} if px else {}
    except (OSError, ValueError):
        return {}

def _env(user):
    uid, gid, home = _user(user)
    return {"HOME": home, "USER": user, "LOGNAME": user, "SHELL": SHELL, "TERM": "xterm",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", **_net_env()}


class Capped:
    """Byte accumulator with a hard cap; remembers that it truncated."""
    def __init__(self, cap=MAX_OUT): self.buf = bytearray(); self.cap = cap; self.truncated = False
    def add(self, b):
        room = self.cap - len(self.buf)
        if room <= 0: self.truncated = True; return b""
        self.buf += b[:room]
        if len(b) > room: self.truncated = True
        return b[:room]
    def text(self): return self.buf.decode(errors="replace")


# ---------------- sessions ----------------
def _take_tty():
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)      # make the pty our controlling terminal (job control works)


class Session:
    def __init__(self, user, use_pty):
        uid, gid, home = _user(user)
        self.id = uuid.uuid4().hex[:8]; self.user = user; self.pty = use_pty; self.alive = True
        self.q = queue.Queue(); self.total = 0
        if use_pty:
            self.mfd, sfd = pty.openpty()
            self.p = subprocess.Popen([SHELL, "-i"], stdin=sfd, stdout=sfd, stderr=sfd, cwd=home,
                                      env=_env(user), user=uid, group=gid, start_new_session=True,
                                      preexec_fn=_take_tty)
            os.close(sfd)
            self.rfd = self.mfd
        else:
            self.p = subprocess.Popen([SHELL, "--norc", "--noprofile"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=home,
                                      env=_env(user), user=uid, group=gid, start_new_session=True, bufsize=0)
            self.rfd = self.p.stdout.fileno()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                r, _, _ = select.select([self.rfd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self.p.poll() is not None and not r:
                break
            if not r:
                continue
            try:
                b = os.read(self.rfd, 65536)
            except OSError:
                break
            if not b:
                break
            self.q.put(b)
        self.alive = False
        self.q.put(None)

    def write(self, data: bytes):
        if self.pty: os.write(self.mfd, data)
        else: self.p.stdin.write(data); self.p.stdin.flush()

    def kill(self):
        self.alive = False
        try: os.killpg(self.p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        try: self.p.wait(timeout=2)
        except Exception: pass
        if self.pty:
            try: os.close(self.mfd)
            except OSError: pass


SESSIONS = {}
SESS_LOCK = threading.Lock()


# ---------------- ops ----------------
def op_ping(req, emit):
    return {"uptime_s": round(time.monotonic() - BOOT_T, 3), "pid": os.getpid()}


def op_health(req, emit):
    la = os.getloadavg(); mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":"); mem[k] = int(v.split()[0])
    return {"uptime_s": round(time.monotonic() - BOOT_T, 3), "load1": la[0],
            "mem_total_kb": mem["MemTotal"], "mem_avail_kb": mem["MemAvailable"],
            "sessions": len(SESSIONS), "root_fs": subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "/"],
                                                                 capture_output=True, text=True).stdout.strip()}


def op_exec(req, emit):
    """One-shot command as `user` (default agent). Streams stdout+stderr chunks if stream=true."""
    user = req.get("user", DEFAULT_USER); uid, gid, home = _user(user)
    t0 = time.monotonic(); timeout = req.get("timeout", 60)
    p = subprocess.Popen([SHELL, "-c", req["cmd"]], cwd=req.get("cwd") or home,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={**_env(user), **req.get("env", {})}, user=uid, group=gid, start_new_session=True)
    if req.get("stdin"): p.stdin.write(req["stdin"].encode())
    p.stdin.close()
    out, err = Capped(), Capped(); fds = {p.stdout.fileno(): ("out", out), p.stderr.fileno(): ("err", err)}
    timed_out = False
    while fds:
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            timed_out = True; os.killpg(p.pid, signal.SIGKILL); break
        r, _, _ = select.select(list(fds), [], [], min(left, 0.5))
        for fd in r:
            b = os.read(fd, 65536)
            if not b: del fds[fd]; continue
            name, cap = fds[fd]; kept = cap.add(b)
            if req.get("stream") and kept: emit({"event": name, "data": kept.decode(errors="replace")})
    p.wait()
    return {"exit": None if timed_out else p.returncode, "timeout": timed_out, "stdout": out.text(),
            "stderr": err.text(), "truncated": out.truncated or err.truncated,
            "elapsed_s": round(time.monotonic() - t0, 3)}


def op_session_open(req, emit):
    s = Session(req.get("user", DEFAULT_USER), bool(req.get("pty", False)))
    with SESS_LOCK: SESSIONS[s.id] = s
    return {"session": s.id, "pty": s.pty}


def _get(req):
    s = SESSIONS.get(req["session"])
    if not s or not s.alive: raise KeyError("no such live session")
    return s


SENT = "__SBX_%s_"
def op_session_exec(req, emit):
    """Run cmd inside a persistent (non-PTY) session; cwd/env persist across calls. Streams output."""
    s = _get(req)
    if s.pty: raise ValueError("use session_write/session_read for pty sessions")
    token = uuid.uuid4().hex[:8]; sent = (SENT % token).encode()
    pat = re.compile(re.escape(sent) + rb"(\d+)__\n")
    s.write(f"{req['cmd']}\nprintf '\\n{SENT % token}%s__\\n' \"$?\"\n".encode())
    t0 = time.monotonic(); timeout = req.get("timeout", 60); cap = Capped(); buf = b""
    while True:
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            s.kill()
            with SESS_LOCK: SESSIONS.pop(s.id, None)
            return {"exit": None, "timeout": True, "session_closed": True, "stdout": cap.text(),
                    "truncated": cap.truncated, "elapsed_s": round(time.monotonic() - t0, 3)}
        try: b = s.q.get(timeout=min(left, 0.5))
        except queue.Empty: continue
        if b is None:
            with SESS_LOCK: SESSIONS.pop(s.id, None)
            return {"exit": None, "session_closed": True, "stdout": cap.text(), "truncated": cap.truncated}
        buf += b
        m = pat.search(buf)
        if m:
            data, rest = buf[:m.start()], buf[m.end():]
            if data.endswith(b"\n"): data = data[:-1]   # the newline we printed before the sentinel
            kept = cap.add(data)
            if kept and req.get("stream"): emit({"event": "out", "data": kept.decode(errors="replace")})
            if rest: s.q.put(rest)
            return {"exit": int(m.group(1)), "stdout": cap.text(), "truncated": cap.truncated,
                    "elapsed_s": round(time.monotonic() - t0, 3)}
        # stream everything except a possible partial sentinel at the tail
        cut = buf.rfind(b"\n__SBX_")
        flush, buf = (buf, b"") if cut < 0 else (buf[:cut], buf[cut:])
        if flush:
            kept = cap.add(flush)
            if kept and req.get("stream"): emit({"event": "out", "data": kept.decode(errors="replace")})


def op_session_write(req, emit):
    s = _get(req); data = base64.b64decode(req["data_b64"]); s.write(data); return {"bytes": len(data)}


def op_session_read(req, emit):
    """Drain buffered output (PTY or pipe), waiting up to `wait` seconds for the first chunk."""
    s = SESSIONS.get(req["session"])
    if not s: raise KeyError("no such session")
    chunks = []; t_end = time.monotonic() + req.get("wait", 0.2); grace = req.get("grace", 0.05)
    while True:
        left = t_end - time.monotonic()
        try: b = s.q.get(timeout=max(left, 0) if not chunks else grace)   # after first chunk keep draining briefly
        except queue.Empty: break
        if b is None: break
        chunks.append(b)
        if sum(map(len, chunks)) >= MAX_OUT: break
    data = b"".join(chunks)
    return {"data_b64": base64.b64encode(data).decode(), "bytes": len(data), "alive": s.alive}


def op_session_close(req, emit):
    s = SESSIONS.pop(req["session"], None)
    if s: s.kill()
    return {"closed": bool(s)}


def op_write_file(req, emit):
    path = req["path"]; data = base64.b64decode(req["data_b64"])
    os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
    with open(path, "wb") as f: f.write(data)
    if "mode" in req: os.chmod(path, int(req["mode"], 8))
    owner = req.get("owner", DEFAULT_USER)
    if owner: uid, gid, _ = _user(owner); os.chown(path, uid, gid)
    return {"bytes": len(data)}


def op_read_file(req, emit):
    with open(req["path"], "rb") as f:
        f.seek(int(req.get("offset", 0))); data = f.read(req.get("max_bytes", MAX_OUT))
    return {"data_b64": base64.b64encode(data).decode(), "bytes": len(data)}


def op_pack(req, emit):
    """Tar this VM's writable layer (the overlay upper dir) so the host can turn it into a read-only layer.
    Whiteouts (char 0:0) and overlay xattrs are included where tar can read them."""
    out = "/run/pack.tar"
    r = subprocess.run(["tar", "--xattrs", "--xattrs-include=*", "--numeric-owner", "-C", "/run/upper/upper",
                        "-cf", out, "."], capture_output=True, text=True)
    if r.returncode not in (0, 1): raise RuntimeError(r.stderr[-500:])
    return {"path": out, "bytes": os.path.getsize(out), "warnings": r.stderr[-300:]}


def op_list_dir(req, emit):
    out = []
    with os.scandir(req["path"]) as it:
        for e in it:
            st = e.stat(follow_symlinks=False)
            out.append({"name": e.name, "dir": e.is_dir(follow_symlinks=False), "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"entries": out}


def op_delete(req, emit):
    import shutil; p = req["path"]
    if os.path.isdir(p) and not os.path.islink(p): shutil.rmtree(p)
    else: os.remove(p)
    return {"deleted": p}


def op_set_time(req, emit):
    """Host pushes its clock after a snapshot restore."""
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    class TS(ctypes.Structure): _fields_ = [("sec", ctypes.c_long), ("nsec", ctypes.c_long)]
    ts = TS(int(req["epoch"]), int((req["epoch"] % 1) * 1e9))
    if libc.clock_settime(0, ctypes.byref(ts)) != 0: raise OSError(ctypes.get_errno(), "clock_settime")
    return {"now": time.time()}


def op_add_entropy(req, emit):
    """Host pushes fresh random bytes after a restore so clones diverge."""
    data = base64.b64decode(req["data_b64"])
    with open("/dev/urandom", "wb") as f: f.write(data)
    return {"bytes": len(data)}


def op_net_config(req, emit):
    """Host pushes this VM's addressing (also after restore/fork, when the IP changes)."""
    ip, prefix, gw, proxy = req["ip"], req["prefix"], req["gw"], req.get("proxy")
    sh = lambda *c: subprocess.run(c, capture_output=True, text=True)
    sh("ip", "addr", "flush", "dev", "eth0"); sh("ip", "link", "set", "eth0", "up")
    sh("ip", "addr", "add", f"{ip}/{prefix}", "dev", "eth0")
    sh("ip", "route", "replace", "default", "via", gw, "dev", "eth0")
    os.makedirs(os.path.dirname(NET_FILE), exist_ok=True)
    json.dump({"ip": ip, "prefix": prefix, "gw": gw, "proxy": proxy}, open(NET_FILE, "w"))
    with open("/etc/resolv.conf", "w") as f: f.write(f"nameserver {gw}\n")        # names are resolved by the proxy
    if proxy:
        with open("/etc/profile.d/10-sandbox-proxy.sh", "w") as f:
            f.write(f'export http_proxy={proxy} https_proxy={proxy} HTTP_PROXY={proxy} HTTPS_PROXY={proxy} no_proxy=localhost,127.0.0.1\n')
        os.makedirs("/etc/apt/apt.conf.d", exist_ok=True)
        with open("/etc/apt/apt.conf.d/99sandbox-proxy", "w") as f:
            f.write(f'Acquire::http::Proxy "{proxy}";\nAcquire::https::Proxy "{proxy}";\n')
    r = sh("ip", "-4", "-br", "addr", "show", "eth0")
    return {"eth0": r.stdout.strip(), "proxy": proxy}


def op_quiesce(req, emit):
    """Before a snapshot: flush and freeze the writable layer so the disk image is consistent."""
    os.sync()
    r = subprocess.run(["fsfreeze", "--freeze", "/run/upper"], capture_output=True, text=True)
    return {"frozen": r.returncode == 0, "detail": r.stderr.strip()[-120:]}


def op_thaw(req, emit):
    """After a restore (or an aborted snapshot): unfreeze the writable layer. Idempotent."""
    r = subprocess.run(["fsfreeze", "--unfreeze", "/run/upper"], capture_output=True, text=True)
    return {"thawed": r.returncode == 0 or "not frozen" in r.stderr, "detail": r.stderr.strip()[-120:]}


def op_shutdown(req, emit):
    def later():
        time.sleep(0.2); os.sync()
        ctypes.CDLL(None, use_errno=True).reboot(0x1234567)   # RESTART; with reboot=k Firecracker exits
    threading.Thread(target=later, daemon=True).start()
    return {"shutting_down": True}


OPS = {k[3:]: v for k, v in globals().items() if k.startswith("op_")}


def serve(conn, peer):
    rf = conn.makefile("rb"); wf = conn.makefile("wb", buffering=0)   # buffered in, unbuffered out
    lock = threading.Lock()
    def send(obj):
        with lock: wf.write((json.dumps(obj) + "\n").encode())
    for line in rf:
        req = None
        try:
            req = json.loads(line); rid = req.get("id")
            emit = (lambda ev: send({"id": rid, **ev}))
            resp = {"id": rid, "ok": True, **OPS[req["op"]](req, emit)}
        except Exception as e:  # noqa: BLE001
            resp = {"id": (req or {}).get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}
        send(resp)
    conn.close()


def main():
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.bind((socket.VMADDR_CID_ANY, PORT)); s.listen(64)
    print(f"sandboxd v1 listening on vsock port {PORT} (host-only)", flush=True)
    while True:
        conn, peer = s.accept()
        if peer[0] != HOST_CID:                     # defense in depth; this kernel has no vsock loopback anyway
            print(f"sandboxd: rejected connection from cid {peer[0]}", flush=True); conn.close(); continue
        threading.Thread(target=serve, args=(conn, peer), daemon=True).start()


if __name__ == "__main__":
    main()
