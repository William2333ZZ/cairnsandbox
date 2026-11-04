"""Client for the Cairn kernel ABI served by agent-runtime on /run/aios/abi.sock (NDJSON over a unix socket).
Optional: when the socket is missing (old base, local runs) every call is a no-op, so the harness runs anywhere."""
import json, os, socket, threading

SOCK = os.environ.get("CAIRN_ABI_SOCK", "/run/aios/abi.sock")


class ABI:
    def __init__(self, path=SOCK):
        self.path, self.s, self.f, self.n, self.lock = path, None, None, 0, threading.Lock()
        self.info = None
        if os.path.exists(path):
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.s.settimeout(5); self.s.connect(path)
                self.f = self.s.makefile("rwb", buffering=0); self.info = self.call("hello")
            except OSError: self.s = None

    @property
    def available(self): return self.s is not None

    def call(self, op, **kw):
        if not self.s: return None
        with self.lock:
            self.n += 1; req = {"id": self.n, "op": op, **kw}
            try:
                self.f.write((json.dumps(req) + "\n").encode()); line = self.f.readline()
            except OSError: self.s = None; return None
        if not line: self.s = None; return None
        r = json.loads(line)
        return r if r.get("ok") else None

    def event(self, type_, **data): return self.call("event", type=type_, data=data)
    def charge(self, tokens_in=0, tokens_out=0, cost_usd=0.0): return self.call("budget_charge", tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)
    def budget(self): return self.call("budget_get")
    def checkpoint(self, note=""): return self.call("checkpoint", note=note)
    def note(self, text): return self.call("event", type="note", data={"text": text[:1000]})          # memory notes layer (docs/005 记忆): the host collects these when the assignment ends
    def report(self, state, text="", attention=None, deliverable=None):
        """Report one of progress|waiting|ready|blocked|failed|dropped to the host (docs/006-execution.md)."""
        kw = {"state": state, "text": text[:2000]}
        if attention: kw["attention"] = attention
        if deliverable: kw["deliverable"] = deliverable
        return self.call("report", **kw)
    def ticket(self):
        r = self.call("ticket_get"); return (r or {}).get("ticket")
    def inbox(self, since=0):
        r = self.call("inbox", since=since); return (r or {}).get("inputs", [])
