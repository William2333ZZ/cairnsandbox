"""One sandbox and the things done to it. Every method talks to the control plane; `info` is the last answer we have about it, `refresh()` asks again."""
import base64, json, time, urllib.error, urllib.parse, urllib.request


class CommandResult:
    def __init__(self, d): self.exit, self.stdout, self.stderr, self.timed_out, self.elapsed_s = d.get("exit"), d.get("stdout", ""), d.get("stderr", ""), bool(d.get("timeout")), d.get("elapsed_s"); self.raw = d
    def check(self):
        """Raise when the command did not exit 0 (like subprocess.run(check=True))."""
        if self.exit != 0: raise RuntimeError(f"exit {self.exit}: {(self.stderr or self.stdout)[-400:]}")
        return self
    def __repr__(self): return f"CommandResult(exit={self.exit}, stdout={self.stdout[:60]!r}, timed_out={self.timed_out})"


class Commands:
    def __init__(self, sb): self.sb = sb
    def run(self, cmd, cwd=None, env=None, timeout=None, user=None, stdin=None, landlock=None):
        """Run a shell command (bash -c) and wait. `user` agent|root; `landlock` True = the harness's default confinement, or {ro, rw, connect_ports}."""
        body = {k: v for k, v in {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout, "user": user, "stdin": stdin, "landlock": landlock}.items() if v is not None}
        return CommandResult(self.sb.c.post(f"/sandboxes/{self.sb.id}/exec", body, timeout=(timeout or 600) + 30))
    def stream(self, cmd, cwd=None, env=None, timeout=None, user=None):
        """Yield ("stdout"|"stderr", text) as the command runs; the last item is ("done", CommandResult)."""
        body = {k: v for k, v in {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout, "user": user}.items() if v is not None}
        resp = self.sb.c.request("POST", f"/sandboxes/{self.sb.id}/exec/stream", body, stream=True, timeout=(timeout or 600) + 30)
        with resp:
            for line in resp:
                if not line.strip(): continue
                ev = json.loads(line)
                if ev.get("done"): yield "done", CommandResult(ev); return
                yield {"out": "stdout", "err": "stderr"}.get(ev.get("event"), ev.get("event")), ev.get("data", "")   # the node says out / err


class Files:
    def __init__(self, sb): self.sb = sb
    def write(self, path, data):
        """Whole file; str is UTF-8. Returns the byte count."""
        if isinstance(data, str): data = data.encode()
        return self.sb.c.put(f"/sandboxes/{self.sb.id}/files?path={urllib.parse.quote(path)}", bytes(data))["bytes"]
    def read(self, path): return self.sb.c.get(f"/sandboxes/{self.sb.id}/files?path={urllib.parse.quote(path)}", raw=True)
    def read_text(self, path, encoding="utf-8"): return self.read(path).decode(encoding)
    def list(self, path="/home/agent"):
        """Names in a directory (a trailing / marks sub-directories)."""
        r = self.sb.commands.run(f"ls -1Ap {json.dumps(path)}").check()
        return [x for x in r.stdout.split("\n") if x]
    def exists(self, path): return self.sb.commands.run(f"test -e {json.dumps(path)}").exit == 0
    def remove(self, path): self.sb.commands.run(f"rm -rf -- {json.dumps(path)}").check()
    def mkdir(self, path): self.sb.commands.run(f"mkdir -p -- {json.dumps(path)}").check()


class Session:
    """A long-lived shell (optionally a PTY): exec keeps state between calls; write/read are the raw byte stream."""
    def __init__(self, sb, sid, pty): self.sb, self.id, self.pty = sb, sid, pty
    def exec(self, cmd, timeout=60): return CommandResult(self.sb.c.post(f"/sandboxes/{self.sb.id}/sessions/{self.id}/exec", {"cmd": cmd, "timeout": timeout}, timeout=timeout + 30))
    def write(self, data):
        if isinstance(data, str): data = data.encode()
        self.sb.c.post(f"/sandboxes/{self.sb.id}/sessions/{self.id}/write", {"data_b64": base64.b64encode(bytes(data)).decode()})
    def read(self, wait=0.5):
        r = self.sb.c.post(f"/sandboxes/{self.sb.id}/sessions/{self.id}/read", {"wait": wait}, timeout=wait + 30)
        return base64.b64decode(r.get("data_b64") or b"")
    def close(self): self.sb.c.delete(f"/sandboxes/{self.sb.id}/sessions/{self.id}")
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class Pty:
    def __init__(self, sb): self.sb = sb
    def open(self, user=None, pty=True):
        r = self.sb.c.post(f"/sandboxes/{self.sb.id}/sessions", {k: v for k, v in {"user": user, "pty": pty}.items() if v is not None})
        return Session(self.sb, r["session"], r.get("pty", pty))


class Ports:
    """A service listening inside the sandbox, reached through the control plane (plain HTTP, or a WebSocket tunnel after the handshake)."""
    def __init__(self, sb): self.sb = sb
    def url(self, port, path="/"): return f"{self.sb.c.url}/sandboxes/{self.sb.id}/ports/{int(port)}{path if path.startswith('/') else '/' + path}"
    def get(self, port, path="/", headers=None):
        """(status, headers, body) of a GET to the guest's port; the bearer is added for you."""
        h = {"Authorization": "Bearer " + self.sb.c.token} if self.sb.c.token else {}
        if self.sb.c.project: h["X-Project"] = self.sb.c.project
        req = urllib.request.Request(self.url(port, path), headers={**h, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r: return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e: return e.code, dict(e.headers), e.read()
    def websocket(self, port, path="/", headers=None):
        """A raw socket on which the WebSocket handshake has already been answered with 101; frame the bytes yourself (or hand it to a websocket library's `sock=`)."""
        import socket
        u = urllib.parse.urlsplit(self.sb.c.url); s = socket.create_connection((u.hostname, u.port or 80), timeout=30)
        hs = f"GET /sandboxes/{self.sb.id}/ports/{int(port)}{path} HTTP/1.1\r\nHost: {u.netloc}\r\n" + (f"Authorization: Bearer {self.sb.c.token}\r\n" if self.sb.c.token else "")
        if self.sb.c.project: hs += f"X-Project: {self.sb.c.project}\r\n"
        hs += "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + base64.b64encode(__import__("os").urandom(16)).decode() + "\r\n"
        hs += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()) + "\r\n"
        s.sendall(hs.encode()); buf = b""
        while b"\r\n\r\n" not in buf:
            d = s.recv(4096)
            if not d: break
            buf += d
        head = buf.split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in head: s.close(); raise RuntimeError(f"no upgrade: {head}")
        return s


class Checkpoints:
    """Points to go back to (docs/002 沙箱侧 5 Checkpoint): taken live, rolled back in place, forked from; they travel with the sandbox when it migrates."""
    def __init__(self, sb): self.sb = sb
    def create(self, name=None, note=None): return self.sb.c.post(f"/sandboxes/{self.sb.id}/checkpoints", {k: v for k, v in {"name": name, "note": note}.items() if v is not None})
    def list(self): return self.sb.c.get(f"/sandboxes/{self.sb.id}/checkpoints")["checkpoints"]
    def rollback(self, name):
        r = self.sb.c.post(f"/sandboxes/{self.sb.id}/rollback", {"checkpoint": name}); self.sb.info = r; return r
    def drop(self, name): return self.sb.c.delete(f"/sandboxes/{self.sb.id}/checkpoints/{name}")


class NetworkPolicy:
    def __init__(self, sb): self.sb = sb
    def get(self): return self.sb.refresh().get("network")
    def set(self, **policy):
        """mode=, allow_hosts=, allow_direct=, rules=, limits=. Tightening applies at once; a member's loosening raises NeedsApproval (its .request waits for an owner)."""
        return self.sb.c.put(f"/sandboxes/{self.sb.id}/network", policy)["network"]
    def decisions(self): return self.sb.c.get(f"/sandboxes/{self.sb.id}/egress")["decisions"]


class Sandbox:
    def __init__(self, client, info):
        self.c, self.info = client, info
        self.commands, self.files, self.pty, self.ports, self.checkpoints, self.network = Commands(self), Files(self), Pty(self), Ports(self), Checkpoints(self), NetworkPolicy(self)

    id = property(lambda self: self.info["id"])
    status = property(lambda self: self.info.get("status"))
    node = property(lambda self: self.info.get("node"))
    def __repr__(self): return f"Sandbox({self.id}, {self.status}, node={self.node})"

    def refresh(self): self.info = self.c.get(f"/sandboxes/{self.id}"); return self.info
    def pause(self): self.info = self.c.post(f"/sandboxes/{self.id}/pause"); return self
    def resume(self): self.info = self.c.post(f"/sandboxes/{self.id}/resume"); return self
    def kill(self): return self.c.delete(f"/sandboxes/{self.id}")
    delete = kill
    def set_timeout(self, ttl_idle_s=None, ttl_max_s=None, retain_paused_s=None):
        """Idle / max TTL and how long a paused sandbox is kept (0 = until deleted). E2B's set_timeout is the idle one."""
        self.info = self.c.post(f"/sandboxes/{self.id}/ttl", {k: v for k, v in {"ttl_idle_s": ttl_idle_s, "ttl_max_s": ttl_max_s, "retain_paused_s": retain_paused_s}.items() if v is not None}); return self
    def fork(self, n=1, checkpoint=None, cls="ephemeral", ttl_idle_s=None, ttl_max_s=None, qos=None):
        """n clones (copy-on-write) from now, or from a checkpoint; the parent keeps running."""
        body = {k: v for k, v in {"n": n, "checkpoint": checkpoint, "class": cls, "ttl_idle_s": ttl_idle_s, "ttl_max_s": ttl_max_s, "qos": qos}.items() if v is not None}
        return [Sandbox(self.c, x) for x in self.c.post(f"/sandboxes/{self.id}/fork", body)["clones"]]
    def pack(self, name): return self.c.post(f"/sandboxes/{self.id}/pack", {"name": name})
    def commit(self, name, note=None):
        """This moment (memory + disk) as a named template new sandboxes boot warm from (a Build; see client.builds.wait)."""
        return self.c.builds.commit(self.id, name, note)
    def migrate(self, to=None): return self.c.post(f"/sandboxes/{self.id}/migrate", {"to": to} if to else {})
    def diff(self, cwd="/work/repo", base="main"): return self.c.get(f"/sandboxes/{self.id}/diff?" + urllib.parse.urlencode({"cwd": cwd, "base": base}))
    def usage(self): return self.c.billing.sandbox_usage(self.id)

    # ---- what the sandbox knows about the agent inside it (the kernel ABI; CONTRACT §7) ----
    def budget(self, max_usd=None, max_tokens=None):
        return self.c.put(f"/sandboxes/{self.id}/budget", {k: v for k, v in {"max_usd": max_usd, "max_tokens": max_tokens}.items() if v is not None})["budget"]
    def ticket(self, ticket): return self.c.put(f"/sandboxes/{self.id}/ticket", {"ticket": ticket})["ticket"]
    def inbox(self, text, kind="note", actor=None, thread=None):
        return self.c.post(f"/sandboxes/{self.id}/inbox", {k: v for k, v in {"kind": kind, "text": text, "actor": actor, "thread": thread}.items() if v is not None})
    def events(self, since=0, max=None):
        """(events, last_seq) from the sandbox's event log (model calls, reports, notes, checkpoints)."""
        r = self.c.get(f"/sandboxes/{self.id}/events?" + urllib.parse.urlencode({"since": since, **({"max": max} if max else {})}))
        return r.get("events", []), r.get("last_seq", since)
    TERMINAL_REPORTS = ("ready", "waiting", "blocked", "failed")

    def wait_for_report(self, timeout=3600, every=2.0, since=0, include_progress=False):
        """Block until the harness files a report that ends its turn (state ready / waiting / blocked / failed; `include_progress=True` also returns on
        progress); returns the event {seq, ts, type: "report", data: {state, text, ...}} or None on timeout."""
        t0 = time.time(); seq = since
        while time.time() - t0 < timeout:
            evs, seq = self.events(since=seq)
            for e in evs:
                if e.get("type") != "report": continue
                if include_progress or (e.get("data") or {}).get("state") in self.TERMINAL_REPORTS: return e
            time.sleep(every)
        return None

    def __enter__(self): return self
    def __exit__(self, *a):
        try: self.kill()
        except Exception: pass  # noqa: BLE001
