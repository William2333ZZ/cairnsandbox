# One Engine per user on the same deployment (docs/009 鉴权与隔离): shared sandboxes, model proxy, code host and evaluator; a
# Store scoped to the user, its own policy, routines, memory, conversation and pass loop. The deployer is user "default"; the
# code host's and CI's triggers belong to the deployer, every user's routines fire for that user.
import threading
from config import *
from clients import CP, ModelProxy, EvalClient
from connectors import make_connector
from chat import ChatConnector
from engine import Engine
from sources import ScheduleSource

USER_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")
NATURAL_HANDOFF_RE = re.compile(r"(?:交给|转给|移交给|hand\s+(?:this\s+)?(?:off\s+)?to|pass\s+(?:this\s+)?to|assign\s+(?:this\s+)?to)\s*@?([a-z][a-z0-9_-]{1,31})\b", re.I)


class Engines:
    def __init__(self, cfg, cp=None, proxy=None, gitea=None, evalc=None, chat=None):
        self.cfg = cfg; self.lock = threading.RLock(); self.by_user = {}
        self.cp = cp or CP(cfg["cp_url"], cfg["cp_token"]); self.proxy = proxy or (ModelProxy(cfg["model_proxy"], cfg["model_name"]) if cfg.get("model_proxy") else None)
        self.gitea = gitea or make_connector(cfg); self.chat = chat or ChatConnector(cfg); self.evalc = evalc or (EvalClient(cfg["eval_url"], cfg.get("cp_token", "")) if cfg.get("eval_url") else None)
        self.default = self.get(cfg.get("user") or "default")

    def get(self, user, agent="main"):
        """The user's agent `agent` (docs/001 多个 agent): "main" is the one everyone has; others are roles the user created.
        Each is its own Engine with its own rows (owner key user/agent), routines, memory and conversation; all share the sandboxes."""
        user = (user or "default").strip(); agent = (agent or "main").strip()
        if user != "default" and not USER_RE.fullmatch(user): raise KeyError(user)
        if agent != "main" and not USER_RE.fullmatch(agent): raise KeyError(agent)
        if agent != "main" and not any(a["name"] == agent for a in self.agents(user)): raise KeyError(agent)
        key = user if agent == "main" else f"{user}/{agent}"
        with self.lock:
            e = self.by_user.get(key)
            if e: return e
            e = Engine({**self.cfg, "user": key, "agent": agent, "owner": user}, cp=self.cp, proxy=self.proxy, gitea=self.gitea, evalc=self.evalc, chat=self.chat)
            e.engines = self                                                                # siblings, for hand-offs (docs/002 Handoff)
            e.start_passes(); e.schedule = ScheduleSource(e); e.schedule.start()             # routines are the agent's own
            self.by_user[key] = e; return e

    def agents(self, user):
        """The user's agents besides main: kept in the main engine's kv so the list survives restarts."""
        main = self.by_user.get(user) or self.get(user)
        return [{"name": "main", "role": main.store.kv_get("agent.role", ""), "created": None}] + (main.store.kv_get("agents", []) or [])

    def agent_put(self, user, name, role=""):
        name = (name or "").strip(); role = (role or "").strip()[:2000]
        if name == "main": self.get(user).store.kv_set("agent.role", role); return self.agents(user)
        if not USER_RE.fullmatch(name): raise ValueError("agent name: lowercase letters, digits, _ or -, 2-32 chars")
        main = self.get(user); rows = [a for a in (main.store.kv_get("agents", []) or []) if a["name"] != name]
        main.store.kv_set("agents", rows + [{"name": name, "role": role, "created": now()}])
        self.get(user, name).store.kv_set("agent.role", role); return self.agents(user)

    def agent_delete(self, user, name):
        if name == "main": raise ValueError("main cannot be deleted")
        main = self.get(user); rows = main.store.kv_get("agents", []) or []
        if not any(a["name"] == name for a in rows): raise KeyError(name)
        main.store.kv_set("agents", [a for a in rows if a["name"] != name])
        gone = self.by_user.pop(f"{user}/{name}", None)
        if gone is not None: gone._stop.set(); getattr(gone, "schedule", None) and gone.schedule._stop.set()          # a deleted agent's pass loop and routines stop with it (a re-created one would otherwise double up)
        return self.agents(user)

    def route(self, user, text):
        """"@fix 修一下登录页" → the user's agent fix and the text without the mention; otherwise main and the text as is."""
        m = re.match(r"^@([a-z][a-z0-9_-]{1,31})\s+(.+)$", (text or "").strip(), re.S)
        if m and any(a["name"] == m.group(1) for a in self.agents(user)): return m.group(1), m.group(2)
        n = NATURAL_HANDOFF_RE.search(text or "")                                         # "请把这件事交给 fix 去做": no @ needed when the person names the agent after a hand-over verb
        if n and n.group(1) != "main" and any(a["name"] == n.group(1) for a in self.agents(user)): return n.group(1), text
        return "main", text

    def users(self): return sorted(self.by_user)
