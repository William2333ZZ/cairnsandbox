# HTTP surface of the Agent service: the route table and the server (CONTRACT.md §8).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from sources import workbench_trigger

# ----------------------------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    e = None                                   # the Engine for the deployer (tests set it directly)
    engines = None                             # Engines: one per user, picked by X-User (set by serve())

    def engine_for(self):
        """The user's Engine: X-User from the control plane (trusted, it comes with the bearer); a bare request is the deployer's."""
        if self.engines is None: return self.e
        user = (self.headers.get("X-User") or "").strip() or "default"
        return self.engines.get(user, (self.headers.get("X-Agent") or "").strip() or "main")
    protocol_version = "HTTP/1.1"
    ROUTES = [
        ("GET", r"/health", lambda e, m, b, q: e.health()),
        ("GET", r"/attention", lambda e, m, b, q: e.attention()),
        ("GET", r"/conversation", lambda e, m, b, q: e.conversation_feed(q.get("since") or 0, q.get("limit") or 200)),
        ("GET", r"/approvals", lambda e, m, b, q: {"approvals": e.approvals(q.get("state"))}),
        ("GET", r"/memory", lambda e, m, b, q: e.memory_view()),
        ("GET", r"/corrections", lambda e, m, b, q: e.corrections_view()),
        ("GET", r"/ask", lambda e, m, b, q: e.ask_view()),
        ("GET", r"/routines", lambda e, m, b, q: {"routines": e.routines()}),
        ("GET", r"/connectors", lambda e, m, b, q: e.connectors_view()),
        ("POST", r"/hooks/chat", lambda e, m, b, q: e.chat_hook(q.get("__headers__") or {}, b)),
        ("POST", r"/connectors/([^/]+)/pause", lambda e, m, b, q: e.connector_pause(m[0], b.get("paused", True))),
        ("GET", r"/agent", lambda e, m, b, q: e.agent_view()),
        ("POST", r"/agent/version", lambda e, m, b, q: e.set_agent_version(b.get("version", "previous") if "version" in b else "previous", pin=b.get("pin"))),
        ("POST", r"/updates", lambda e, m, b, q: (201, e.update(b))),
        ("POST", r"/routines/([^/]+)", lambda e, m, b, q: e.routine_update(m[0], b)),
        ("PUT", r"/memory/profile", lambda e, m, b, q: e.memory_put_profile(b.get("facts") or [])),
        ("POST", r"/memory/profile", lambda e, m, b, q: (e.store.insert("memory", {"id": new_id("m"), "layer": "profile", "text": (b.get("text") or "").strip()[:200], "ticket": None, "repo": None, "source": "person", "created": now(), "updated": now()}) if (b.get("text") or "").strip() else None, e.memory_view())[1]),
        ("DELETE", r"/memory/([^/]+)", lambda e, m, b, q: e.memory_delete(m[0])),
        ("POST", r"/approvals/([^/]+)", lambda e, m, b, q: e.decide_approval(m[0], b.get("decision", ""), actor=b.get("actor") or "panel", reason=(b.get("reason") or "")[:500], override=bool(b.get("override")))),
        ("POST", r"/conversation", lambda e, m, b, q: (201, e.converse_in(b))),
        ("GET", r"/agents", lambda e, m, b, q: {"agents": Handler.engines.agents(e.cfg.get("owner") or e.user) if Handler.engines else [{"name": "main", "role": e.store.kv_get("agent.role", "")}], "current": e.cfg.get("agent") or "main"}),
        ("POST", r"/agents", lambda e, m, b, q: (201, {"agents": Handler.engines.agent_put(e.cfg.get("owner") or e.user, b.get("name", ""), b.get("role", ""))}) if Handler.engines else (400, {"error": "single engine"})),
        ("DELETE", r"/agents/([^/]+)", lambda e, m, b, q: {"agents": Handler.engines.agent_delete(e.cfg.get("owner") or e.user, m[0])} if Handler.engines else (400, {"error": "single engine"})),
        ("GET", r"/sources", lambda e, m, b, q: {"sources": e.sources_view()}),
        ("POST", r"/requests", lambda e, m, b, q: (201, {"trigger": (t := workbench_trigger(b))["id"], "thread": t["threads"][0], "disposition": e.receive(t)})),
        ("POST", r"/triggers", lambda e, m, b, q: (201, {"disposition": e.receive(b)})),            # a Source pushing a translated Trigger (tests, future webhooks)
        ("GET", r"/triggers/([^/]+)", lambda e, m, b, q: e.store.get("triggers", m[0])),
        ("POST", r"/triggers/([^/]+)/reevaluate", lambda e, m, b, q: e.reevaluate(m[0])),
        ("GET", r"/threads", lambda e, m, b, q: {"threads": e.store.all("threads", order="updated DESC")}),
        ("GET", r"/threads/(.+)", lambda e, m, b, q: e.thread_view(urllib.parse.unquote(m[0]))),
        ("GET", r"/decisions", lambda e, m, b, q: {"decisions": e.store.all("decisions", order="created DESC")[:200]}),
        ("GET", r"/tickets", lambda e, m, b, q: {"tickets": [e.ticket_view(t["id"], full=False) for t in e.store.all("tickets", *(("WHERE state=?", (q["state"],)) if q.get("state") else ()), order="updated DESC")]}),
        ("GET", r"/tickets/([^/]+)", lambda e, m, b, q: e.ticket_view(m[0])),
        ("GET", r"/prompts", lambda e, m, b, q: e.prompts_view()),
        ("PUT", r"/prompts/([^/]+)", lambda e, m, b, q: e.prompts_save(m[0], b.get("text"), base=b.get("base"), actor=b.get("actor", "panel"))),
        ("DELETE", r"/prompts/([^/]+)", lambda e, m, b, q: e.prompts_reset(m[0])),
        ("GET", r"/home", lambda e, m, b, q: e.home_view()),                                   # the person's harness directory (docs/004, D29)
        ("GET", r"/home/file", lambda e, m, b, q: e.home_read(q.get("path", ""))),
        ("PUT", r"/home/file", lambda e, m, b, q: e.home_write(b.get("path", ""), b.get("text"), actor=b.get("actor", "panel"))),
        ("DELETE", r"/home/file", lambda e, m, b, q: e.home_delete(q.get("path", ""), actor=q.get("actor", "panel"))),
        ("POST", r"/tickets/([^/]+)/subscribe", lambda e, m, b, q: e.subscribe(m[0], b["thread"], b.get("mode", "own"), source=b.get("source"))),
        ("POST", r"/tickets/([^/]+)/stop", lambda e, m, b, q: e.stop_ticket(m[0], by="person", reason=b.get("reason", ""))),
        ("POST", r"/tickets/([^/]+)/retry", lambda e, m, b, q: e.retry_ticket(m[0])),
        ("POST", r"/tickets/([^/]+)/reply", lambda e, m, b, q: e.answer_ticket(m[0], b.get("text", ""), actor=b.get("actor", "panel"))),
        ("GET", r"/tickets/([^/]+)/live", lambda e, m, b, q: e.live(m[0])),
        ("POST", r"/tickets/([^/]+)/mine", lambda e, m, b, q: e.mine_again(m[0])),          # retry mining a merged job (docs/007) after the evaluator was unreachable
        ("GET", r"/tickets/([^/]+)/conversation", lambda e, m, b, q: e.conversation(m[0])),
        ("GET", r"/workspaces", lambda e, m, b, q: {"workspaces": [{"id": "gitea:" + e.gitea.repo if e.gitea.repo else "workbench", "title": e.gitea.repo or "工作台", "source": "gitea" if e.gitea.readable else "workbench"}]}),
        ("GET", r"/assignments/([^/]+)", lambda e, m, b, q: e.assignment_view(m[0])),
        ("GET", r"/policy", lambda e, m, b, q: {"version": e.policy.version, **e.policy.p}),
        ("PUT", r"/policy", lambda e, m, b, q: e.policy.update(b)),
        ("GET", r"/policy/versions", lambda e, m, b, q: {"versions": e.store.all("policy_versions", order="created DESC")}),
        ("POST", r"/pause", lambda e, m, b, q: e.set_paused(b.get("paused", True))),
        ("POST", r"/pass", lambda e, m, b, q: (e.pass_once(), {"ok": True})[1]),
    ]

    def log_message(self, *a): pass

    def _send(self, code, obj):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _dispatch(self, method):
        u = urllib.parse.urlparse(self.path); path = u.path.rstrip("/") or "/"; q = dict(urllib.parse.parse_qsl(u.query))
        n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n) if n else b""
        try: e = self.engine_for()
        except KeyError: return self._send(400, {"error": "bad user or agent"})
        tok = e.cfg.get("cp_token")
        if tok and path not in ("/health", "/hooks/chat") and not hmac.compare_digest(self.headers.get("Authorization") or "", f"Bearer {tok}"): return self._send(401, {"error": "bad token"})
        if path == "/hooks/chat": q["__headers__"] = {k: v for k, v in self.headers.items()}
        try: body = json.loads(raw) if raw else {}
        except ValueError: return self._send(400, {"error": "bad json"})
        if self.engines is not None and method == "POST" and path == "/conversation" and not (body or {}).get("ticket") and (body or {}).get("text"):
            owner = e.cfg.get("owner") or e.user; name, text = self.engines.route(owner, body["text"])
            if name != "main": e = self.engines.get(owner, name); body = {**body, "text": text}      # "@fix …" in the conversation goes to that agent (docs/001 多个 agent)
        for meth, pat, fn in self.ROUTES:
            m = re.fullmatch(pat, path)
            if meth == method and m:
                try:
                    r = fn(e, m.groups(), body, q)
                    code, obj = r if isinstance(r, tuple) else (200, r)
                    return self._send(404, {"error": "not found"}) if obj is None else self._send(code, obj)
                except PermissionError as e: return self._send(403, {"error": str(e)})
                except KeyError as e: return self._send(404, {"error": str(e)})
                except ValueError as e: return self._send(400, {"error": str(e)})
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "no route"})

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PUT(self): self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")


def serve(e, port, engines=None):
    Handler.e = e; Handler.engines = engines
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler); srv.daemon_threads = True
    print(f"fc-intake listening on :{port} state={e.cfg['state']} gitea={'on' if e.gitea.enabled else 'off'} mode={e.policy.p['mode']}", flush=True)
    return srv
