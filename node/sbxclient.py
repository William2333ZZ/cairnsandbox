#!/usr/bin/env python3
"""Host-side client for sandboxd over Firecracker's vsock UDS (CONNECT <port> handshake)."""
import base64, json, socket, time, uuid

VSOCK_PORT = 5000


class Guest:
    def __init__(self, uds, timeout=None):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout); self.s.connect(uds)
        self.s.sendall(f"CONNECT {VSOCK_PORT}\n".encode())
        self.rf = self.s.makefile("rb")                 # buffered reads: an unbuffered readline() recv()s one byte at a time
        self.wf = self.s.makefile("wb", buffering=0)
        ack = self.rf.readline()
        if not ack.startswith(b"OK "): raise RuntimeError(f"vsock connect failed: {ack!r}")

    def call(self, op, on_event=None, **kw):
        req = {"id": uuid.uuid4().hex[:8], "op": op, **kw}
        self.wf.write((json.dumps(req) + "\n").encode())
        while True:
            line = self.rf.readline()
            if not line: raise ConnectionError("guest closed connection")
            msg = json.loads(line)
            if msg.get("id") != req["id"]: continue          # stray message: ignore
            if "event" in msg:
                if on_event: on_event(msg)
                continue
            if not msg.get("ok", False): raise RuntimeError(f"{op}: {msg.get('error')}")
            return msg

    # conveniences
    def exec(self, cmd, **kw): return self.call("exec", cmd=cmd, **kw)
    def write_file(self, path, data: bytes, **kw): return self.call("write_file", path=path, data_b64=base64.b64encode(data).decode(), **kw)
    def read_file(self, path, **kw): return base64.b64decode(self.call("read_file", path=path, **kw)["data_b64"])
    def session_open(self, **kw): return self.call("session_open", **kw)["session"]
    def session_exec(self, sid, cmd, **kw): return self.call("session_exec", session=sid, cmd=cmd, **kw)
    def session_write(self, sid, data: bytes): return self.call("session_write", session=sid, data_b64=base64.b64encode(data).decode())
    def session_read(self, sid, wait=0.2): return base64.b64decode(self.call("session_read", session=sid, wait=wait)["data_b64"])
    def session_close(self, sid): return self.call("session_close", session=sid)
    def close(self):
        try: self.s.close()
        except OSError: pass


def wait_for_guest(uds, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            g = Guest(uds); g.call("ping"); return g, time.monotonic() - t0
        except Exception:  # noqa: BLE001
            time.sleep(0.02)
    raise TimeoutError("sandboxd did not come up")
