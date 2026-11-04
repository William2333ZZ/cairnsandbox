"""HTTP client and the top-level collections (sandboxes, volumes, templates, layers, builds, billing, projects)."""
import json, os, urllib.error, urllib.parse, urllib.request

from .sandbox import Sandbox


class ApiError(Exception):
    """The control plane answered with an error status. `.status`, `.body` (the parsed JSON when there was one)."""
    def __init__(self, status, message, body=None, method="", path=""):
        super().__init__(f"{method} {path} -> {status}: {message}"); self.status, self.message, self.body, self.method, self.path = status, message, body, method, path

class NotFound(ApiError): pass
class Forbidden(ApiError): pass
class NoCapacity(ApiError): pass          # 429: no node with room, a volume busy, a quota of sandboxes
class QuotaExceeded(ApiError): pass       # 402: the project's daily budget is spent
class NeedsApproval(ApiError):
    """202: the change loosens a sandbox's egress and waits for a project owner; `.request` is the pending request."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw); self.request = (self.body or {}).get("request")


def _raise(method, path, status, body):
    msg = body.get("error") if isinstance(body, dict) else None
    msg = msg or (body if isinstance(body, str) else json.dumps(body)[:200])
    cls = {404: NotFound, 403: Forbidden, 429: NoCapacity, 402: QuotaExceeded}.get(status, ApiError)
    raise cls(status, msg, body, method, path)


class Client:
    """One control plane, one bearer token, optionally one project (sent as X-Project; the token's default project otherwise)."""
    def __init__(self, url=None, token=None, project=None, timeout=600):
        self.url = (url or os.environ.get("CAIRN_URL") or "http://127.0.0.1:18700").rstrip("/")
        self.token = token if token is not None else os.environ.get("CAIRN_TOKEN", "")
        self.project = project if project is not None else os.environ.get("CAIRN_PROJECT") or None
        self.timeout = timeout
        self.sandboxes = Sandboxes(self); self.volumes = Volumes(self); self.templates = Templates(self); self.layers = Layers(self)
        self.builds = Builds(self); self.billing = Billing(self); self.projects = Projects(self); self.network = Network(self)

    # ---- transport ----
    def request(self, method, path, body=None, raw=False, headers=None, timeout=None, stream=False):
        """JSON in (dict) or bytes in; JSON out (dict/list) or bytes out when raw=True. Raises ApiError subclasses on 4xx/5xx; 202 raises NeedsApproval."""
        data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/octet-stream" if isinstance(body, (bytes, bytearray)) else "application/json", **(headers or {})}
        if self.token: h["Authorization"] = "Bearer " + self.token
        if self.project: h["X-Project"] = self.project
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            payload = e.read()
            try: parsed = json.loads(payload) if payload else {}
            except ValueError: parsed = payload.decode(errors="replace")
            _raise(method, path, e.code, parsed)
        except urllib.error.URLError as e:
            raise ApiError(0, f"transport: {e.reason}", None, method, path) from None
        if stream: return resp
        with resp:
            payload = resp.read(); status = resp.status
        if raw: return payload
        out = json.loads(payload) if payload else {}
        if status == 202: raise NeedsApproval(202, "waiting for an owner's approval", out, method, path)
        return out

    def get(self, path, **kw): return self.request("GET", path, **kw)
    def post(self, path, body=None, **kw): return self.request("POST", path, body, **kw)
    def put(self, path, body=None, **kw): return self.request("PUT", path, body, **kw)
    def delete(self, path, **kw): return self.request("DELETE", path, **kw)

    def me(self): return self.get("/me")
    def health(self): return self.get("/health")
    def nodes(self): return self.get("/nodes")["nodes"]


class Sandboxes:
    def __init__(self, c): self.c = c

    def create(self, template=None, mem=None, vcpu=None, disk=None, cls=None, network=None, env=None, volumes=None, layers=None, template_key=None,
               ttl_idle_s=None, ttl_max_s=None, retain_paused_s=None, priority=None, qos=None, boot=None, durable=None, node=None, metadata=None, **extra):
        """A new sandbox (CONTRACT §1 POST /sandboxes). Unset arguments are left to the server's defaults. `cls` is persistent|ephemeral; `priority` interactive|batch|background;
        `volumes` is a list of ids or {id, mount, ro}; `network` is {mode, allow_hosts, allow_direct, rules, limits}."""
        body = {k: v for k, v in {"template": template, "mem": mem, "vcpu": vcpu, "disk": disk, "class": cls, "network": network, "env": env, "volumes": volumes, "layers": layers,
                                  "template_key": template_key, "ttl_idle_s": ttl_idle_s, "ttl_max_s": ttl_max_s, "retain_paused_s": retain_paused_s, "priority": priority, "qos": qos,
                                  "boot": boot, "durable": durable, "node": node, "metadata": metadata, **extra}.items() if v is not None}
        return Sandbox(self.c, self.c.post("/sandboxes", body))

    def get(self, sid): return Sandbox(self.c, self.c.get(f"/sandboxes/{sid}"))
    def list(self, status=None, metadata=None, q=None, limit=None, offset=None):
        """Sandboxes of the project. status: a name or a list; metadata: {key: value} that all must match; q: substring of id / template / node / metadata; limit / offset: newest first (D70)."""
        qs = [("status", x) for x in ([status] if isinstance(status, str) else status or [])] + [("metadata", f"{k}:{v}") for k, v in (metadata or {}).items()]
        qs += [(k, v) for k, v in (("q", q), ("limit", limit), ("offset", offset)) if v is not None]
        out = self.c.get("/sandboxes" + ("?" + urllib.parse.urlencode(qs) if qs else ""))
        return [Sandbox(self.c, x) for x in (out["sandboxes"] if isinstance(out, dict) else out)]
    def import_bundle(self, bundle, node=None):
        """Re-home a durable sandbox from its last published bundle (after a node was lost and nobody evacuated it)."""
        return Sandbox(self.c, self.c.post("/sandboxes/import", {k: v for k, v in {"bundle": bundle, "node": node}.items() if v is not None}))


class Volumes:
    def __init__(self, c): self.c = c
    def create(self, name, size_mib=1024, project=None, node=None):
        return self.c.post("/volumes", {k: v for k, v in {"name": name, "size_mib": size_mib, "project": project, "node": node}.items() if v is not None})
    def from_archive(self, name, tar_bytes, size_mib=None, node=None):
        """A volume whose filesystem holds the tar's tree (a harness directory, a cache), made without mounting."""
        q = {"name": name, **({"size_mib": size_mib} if size_mib else {}), **({"node": node} if node else {})}
        return self.c.post("/volumes/from-archive?" + urllib.parse.urlencode(q), tar_bytes)
    def list(self): return self.c.get("/volumes")["volumes"]
    def get(self, vid): return self.c.get(f"/volumes/{vid}")
    def delete(self, vid): return self.c.delete(f"/volumes/{vid}")


class Templates:
    def __init__(self, c): self.c = c
    def list(self): return self.c.get("/templates")["templates"]
    def build(self, name, base=None, run=(), files=None, env=None, mem=1024, allow=(), layer_name=None, note=None, timeout=1800, log=print):
        """Make a warm template in one call (D69): `base` is an OCI image (python:3.12-slim) turned into a layer, a sandbox is started on it, `files` ({path: text}) are written,
        each command in `run` is executed (and must succeed), then the sandbox is committed as template `name` and thrown away. `allow` = hosts the setup commands may reach.
        Returns the finished commit build; raises RuntimeError with the failing command's output. `log` receives progress lines (pass None for silence)."""
        say = log or (lambda *_: None); c = self.c; layer = None
        if base:
            say(f"== image {base} -> layer")
            b = c.builds.wait(c.builds.from_oci(base, name=layer_name or name + "-base")["id"], timeout=timeout)
            if b.get("status") != "done": raise RuntimeError(f"image build failed: {b.get('error')}")
            layer = b["result"]["id"]
        say("== preparing sandbox")
        sb = c.sandboxes.create(layers=[layer] if layer else None, mem=mem, cls="persistent", env=env, network={"mode": "allowlist", "allow_hosts": list(allow)} if allow else None)
        try:
            for path, text in (files or {}).items():
                sb.files.write(path, text); say(f"wrote {path}")
            for cmd in run:
                say(f"$ {cmd}"); r = sb.commands.run(cmd, timeout=timeout)
                if r.stdout.strip(): say(r.stdout.rstrip()[-2000:])
                if r.exit != 0: raise RuntimeError(f"`{cmd}` exited {r.exit}: {(r.stderr or r.stdout)[-600:]}")
            say(f"== commit as {name}")
            done = c.builds.wait(sb.commit(name, note=note)["id"], timeout=timeout)
            if done.get("status") != "done": raise RuntimeError(f"commit failed: {done.get('error')}")
            return done
        finally:
            try: sb.kill()
            except Exception: pass  # noqa: BLE001
    def delete(self, name): return self.c.delete(f"/templates/{name}")      # frees the name; sandboxes already started from it keep running
    def everywhere(self, spec): return self.c.post("/templates/everywhere", spec)

class Layers:
    def __init__(self, c): self.c = c
    def list(self): return self.c.get("/layers")["layers"]
    def everywhere(self, lid): return self.c.post(f"/layers/{lid}/everywhere")

class Builds:
    """Builds (docs/002 沙箱侧 4): an OCI image becomes a layer, a running sandbox becomes a named template."""
    def __init__(self, c): self.c = c
    def from_oci(self, image, name=None, node=None): return self.c.post("/builds", {k: v for k, v in {"kind": "oci", "image": image, "name": name, "node": node}.items() if v is not None})
    def from_archive(self, tar_bytes, name, node=None):
        return self.c.post("/builds/archive?" + urllib.parse.urlencode({"name": name, **({"node": node} if node else {})}), tar_bytes)
    def commit(self, sandbox_id, name, note=None): return self.c.post("/builds", {k: v for k, v in {"kind": "commit", "sandbox": sandbox_id, "name": name, "note": note}.items() if v is not None})
    def get(self, bid): return self.c.get(f"/builds/{bid}")
    def list(self): return self.c.get("/builds")["builds"]
    def log(self, bid, since=0):
        """The node's progress lines for a build (oci builds): {"lines": [...], "next": n}; pass next back as `since` to get only what is new."""
        return self.c.get(f"/builds/{bid}/log?since={int(since)}")
    def wait(self, bid, timeout=1800, every=2.0):
        import time
        t0 = time.time()
        while True:
            b = self.get(bid)
            if b.get("status") in ("done", "failed") or time.time() - t0 > timeout: return b
            time.sleep(every)


class Billing:
    def __init__(self, c): self.c = c
    def _p(self, project): return project or self.c.project or "default"
    def today(self, project=None): return self.c.get(f"/metering/projects/{self._p(project)}/today")["spent_usd"]
    def statement(self, project=None, since=None, until=None):
        q = {k: v for k, v in {"from": since, "to": until}.items() if v is not None}
        return self.c.get(f"/metering/projects/{self._p(project)}/statement" + ("?" + urllib.parse.urlencode(q) if q else ""))
    def statement_csv(self, project=None, since=None, until=None):
        q = {"format": "csv", **{k: v for k, v in {"from": since, "to": until}.items() if v is not None}}
        return self.c.get(f"/metering/projects/{self._p(project)}/statement?" + urllib.parse.urlencode(q), raw=True).decode()
    def settle(self, since, until, project=None): return self.c.post(f"/metering/projects/{self._p(project)}/statements", {"from": since, "to": until})
    def statements(self, project=None): return self.c.get(f"/metering/projects/{self._p(project)}/statements")["statements"]
    def prices(self): return self.c.get("/metering/prices")
    def sandbox_usage(self, sid): return self.c.get(f"/metering/sandboxes/{sid}")["usage"]


class Projects:
    """Tenancy (docs/002 沙箱侧 1): accounts, projects, members, quotas, the project's default network policy."""
    def __init__(self, c): self.c = c
    def _p(self, project): return project or self.c.project or "default"
    def list(self, account=None): return self.c.get(f"/accounts/{account or self.c.me().get('account') or 'default'}/projects")["projects"]
    def quota(self, project=None): return self.c.get(f"/projects/{self._p(project)}/quota")
    def set_quota(self, project=None, **q): return self.c.put(f"/projects/{self._p(project)}/quota", q)
    def members(self, project=None): return self.c.get(f"/projects/{self._p(project)}/members")["members"]
    def add_member(self, user, role="member", name=None, project=None):
        return self.c.post(f"/projects/{self._p(project)}/members", {k: v for k, v in {"user": user, "role": role, "name": name}.items() if v is not None})
    def keys(self, project=None): return self.c.get(f"/projects/{self._p(project)}/keys")["keys"]
    def create_key(self, name, role="member", ttl_s=None, project=None):
        """A named key that reaches this project only (role member | viewer). Returns {"key": {...}, "secret": "cairn_..."}: the secret is shown once."""
        return self.c.post(f"/projects/{self._p(project)}/keys", {k: v for k, v in {"name": name, "role": role, "ttl_s": ttl_s}.items() if v is not None})
    def revoke_key(self, key_id, project=None): return self.c.delete(f"/projects/{self._p(project)}/keys/{key_id}")
    def default_network(self, project=None): return self.c.get(f"/projects/{self._p(project)}/network")["network"]
    def set_default_network(self, network, project=None): return self.c.put(f"/projects/{self._p(project)}/network", network)


class Network:
    """What waits for an owner: egress relaxations asked by members (CONTRACT: /network/requests)."""
    def __init__(self, c): self.c = c
    def requests(self, all=False): return self.c.get("/network/requests" + ("?all=1" if all else ""))["requests"]
    def approve(self, rid): return self.c.post(f"/network/requests/{rid}/approve")["request"]
    def deny(self, rid): return self.c.post(f"/network/requests/{rid}/deny")["request"]
