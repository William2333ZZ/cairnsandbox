# Clients of the other services: the control plane (sandboxes) and the model proxy; InfraError marks failures that retry.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *

# ----------------------------------------------------------------------------------------------- control plane + model proxy

class InfraError(Exception):
    """Sandbox creation/transport/CP errors: retried, reported as infra, never as the agent's fault."""


class CP:
    def __init__(self, url, token=""): self.url, self.token = url.rstrip("/"), token

    def _req(self, method, path, body=None, raw=False, timeout=600):
        data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/octet-stream" if isinstance(body, (bytes, bytearray)) else "application/json")
        if self.token: req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read(); return payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e: raise InfraError(f"CP {method} {path} -> {e.code}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, OSError, ValueError) as e: raise InfraError(f"CP {method} {path} transport error: {e}") from None

    def create(self, spec):
        info = self._req("POST", "/sandboxes", spec)
        if not info.get("id"): raise InfraError(f"create returned no id: {info}")
        return info["id"], info.get("node")

    def put(self, sid, path, data): self._req("PUT", f"/sandboxes/{sid}/files?path={urllib.parse.quote(path)}", data)
    def get(self, sid, path): return self._req("GET", f"/sandboxes/{sid}/files?path={urllib.parse.quote(path)}", raw=True)

    def exec(self, sid, cmd, user="agent", timeout=60, env=None, cwd=None, landlock=None):
        body = {"cmd": cmd, "user": user, "timeout": timeout}
        if env: body["env"] = env
        if cwd: body["cwd"] = cwd
        if landlock is not None: body["landlock"] = landlock
        r = self._req("POST", f"/sandboxes/{sid}/exec", body, timeout=timeout + 60)
        return {"exit": r.get("exit", -1), "stdout": r.get("stdout", ""), "stderr": r.get("stderr", ""), "timeout": bool(r.get("timeout")), "elapsed_s": r.get("elapsed_s")}

    def events(self, sid, since=0):
        try: return self._req("GET", f"/sandboxes/{sid}/events?since={since}&max=2000").get("events", [])
        except InfraError: return []

    def events_strict(self, sid):
        """The event log at the end of an assignment: a failure here is an infra failure, never an empty report."""
        err = None
        for attempt in range(3):
            try: return self._req("GET", f"/sandboxes/{sid}/events?since=0&max=2000").get("events", [])
            except InfraError as e: err = e; time.sleep(2)
        raise InfraError(f"could not read the kernel event log: {err}")

    def ticket(self, sid, ticket): self._req("PUT", f"/sandboxes/{sid}/ticket", {"ticket": ticket})
    # volumes (docs/002 沙箱侧 5; the harness directory as a read-only volume, D29 的下一步)
    def volumes(self): return (self._req("GET", "/volumes") or {}).get("volumes", [])
    def volume_from_archive(self, name, tar, size_mib=64):
        return self._req("POST", f"/volumes/from-archive?name={urllib.parse.quote(name)}&size_mib={int(size_mib)}", tar)
    def volume_delete(self, vid): return self._req("DELETE", f"/volumes/{vid}")
    def inbox(self, sid, item): return self._req("POST", f"/sandboxes/{sid}/inbox", item)
    def budget(self, sid, max_usd): self._req("PUT", f"/sandboxes/{sid}/budget", {"max_usd": max_usd})
    def pack(self, sid, name): return self._req("POST", f"/sandboxes/{sid}/pack", {"name": name}, timeout=300)
    def info(self, sid):
        try: return self._req("GET", f"/sandboxes/{sid}")
        except InfraError: return None

    def destroy(self, sid):
        try: self._req("DELETE", f"/sandboxes/{sid}")
        except InfraError: pass


class ModelProxy:
    def __init__(self, url, name): self.url, self.name = url.rstrip("/"), name

    def _json(self, method, path, body=None, headers=None, timeout=60):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e: raise InfraError(f"model proxy {method} {path} -> {e.code}: {e.read()[:200]!r}") from None
        except (urllib.error.URLError, OSError, ValueError) as e: raise InfraError(f"model proxy {method} {path}: {e}") from None

    def token(self, sandbox, budget_usd):
        r = self._json("POST", "/tokens", {"sandbox": sandbox, "budget_usd": budget_usd}); return r.get("token") or ""

    def usage(self, sandbox):
        try: return self._json("GET", f"/usage/{urllib.parse.quote(sandbox)}")
        except InfraError: return {}

    def complete(self, token, messages, max_tokens=300):
        r = self._json("POST", "/v1/chat/completions", {"model": self.name, "messages": messages, "max_tokens": max_tokens, "temperature": 0},
                       headers={"Authorization": "Bearer " + token}, timeout=120)
        return ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


class EvalClient:
    """The evaluator, for the one thing the Agent service tells it: a task mined from a merged pull request (docs/007)."""
    def __init__(self, url, token=""): self.url, self.token = url.rstrip("/"), token

    def import_task(self, name, payload):
        req = urllib.request.Request(f"{self.url}/tasksets/{urllib.parse.quote(name)}/import", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=300) as r: return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e: raise InfraError(f"evaluator import -> {e.code}: {e.read()[:200]!r}") from None
        except (urllib.error.URLError, OSError, ValueError) as e: raise InfraError(f"evaluator import: {e}") from None
