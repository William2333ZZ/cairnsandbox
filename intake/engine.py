# The Agent service engine: the mixins composed, plus the pass loop and recovery (docs/005 Agent 服务持有什么).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import CP, ModelProxy, EvalClient, InfraError
from store import Store
from policy import Policy
from connectors import make_connector
from chat import ChatConnector
from router import RouterMixin
from gate import GateMixin
from executor import ExecutorMixin
from relay import RelayMixin
from memory import MemoryMixin
from agentstate import AgentStateMixin
from views import ViewsMixin
from mining import MiningMixin
from corrections import CorrectionsMixin
from review import ReviewMixin
from handoff import HandoffMixin
from intent import IntentMixin
from commands import CommandsMixin
from prompts import PromptsMixin
from home import HomeMixin


class Engine(RouterMixin, GateMixin, ExecutorMixin, RelayMixin, MemoryMixin, AgentStateMixin, ViewsMixin, MiningMixin, CorrectionsMixin, ReviewMixin, HandoffMixin, IntentMixin, CommandsMixin, PromptsMixin, HomeMixin):
    """Owns the store and every write to triggers/threads/decisions/tickets/assignments."""

    def __init__(self, cfg, store=None, cp=None, proxy=None, gitea=None, evalc=None, chat=None):
        self.cfg = cfg; os.makedirs(cfg["state"], exist_ok=True)
        self.store = store or Store(os.path.join(cfg["state"], "intake.sqlite"), user=cfg.get("user") or "default")
        self.user = self.store.user
        self.evalc = evalc or (EvalClient(cfg["eval_url"], cfg.get("cp_token", "")) if cfg.get("eval_url") else None)
        self._passes_started = False
        self.policy = Policy(self.store)
        self.cp = cp or CP(cfg["cp_url"], cfg["cp_token"])
        self.proxy = proxy if proxy is not None else (ModelProxy(cfg["model_proxy"], cfg["model_name"]) if cfg["model_proxy"] else None)
        self.gitea = gitea or make_connector(cfg)                                     # the code host Connector (Gitea or GitHub); `gitea` is its historical name here
        self.chat = chat or ChatConnector(cfg)                                         # the chat tool Connector (docs/002)
        self.lock = threading.RLock()
        self.paused = bool(self.store.kv_get("paused", False))
        self.problems = {}                       # name -> {"error", "since"}
        self.running = {}                        # ticket id -> sandbox id (live assignments)
        self.slots = threading.BoundedSemaphore(cfg["max_parallel"])
        self.reserve = threading.BoundedSemaphore(1)
        self._stop = threading.Event()
        self.recover()

    def recover(self):
        """After a restart: running Assignments are over (their sandboxes die with their TTL), running Eval stamps are stale,
        and open Tickets holding undelivered human Inputs get a recovery Assignment (docs/006 失败与恢复)."""
        for a in self.store.all("assignments", "WHERE state IN ('pending','running')"):
            self.store.update("assignments", a["id"], {"state": "ended", "ended": now(), "error": "host restarted"})
            if a.get("sandbox"): self.cp.destroy(a["sandbox"])
        for ap in self.store.all("approvals", "WHERE state='deciding'"):
            self.store.update("approvals", ap["id"], {"state": "pending", "error": "interrupted by a restart; check whether it already happened before trying again"})       # a restart in the middle of an action: back to a draft the person can see
        for th in self.store.all("threads"):
            ev = th.get("eval")
            if ev and ev.get("running"): ev.pop("running", None); self.store.update("threads", th["id"], {"eval": ev})
        for t in self.store.all("tickets", "WHERE state='open'"):
            if any(i.get("human") and not i.get("delivered") for i in (t.get("inputs") or [])): self._new_assignment(t["id"], recovery=True)

    # ---- passes
    def pass_once(self):
        if self.paused: return
        self.evaluate_due(); self.flush_outbox(); self.follow_prs(); self.digests(); self.expire_approvals(); self.handoff_check()
        if now() - getattr(self, "_wd_at", 0) > 60: self._wd_at = now(); self.watchdog()

    def start_passes(self):
        if self._passes_started: return
        self._passes_started = True
        def loop():
            while not self._stop.is_set():
                try: self.pass_once()
                except Exception as e:  # noqa: BLE001
                    self.problems["pass"] = {"error": f"{type(e).__name__}: {e}"[:300], "since": now()}
                self._stop.wait(PASS_EVERY_S)
        threading.Thread(target=loop, daemon=True).start()
