"""The loop. Adapted from mini-swe-agent agents/default.py (MIT): render system + instance prompt, then
query → execute actions → observe, until the environment raises Submitted or a limit is hit."""
import json, logging, threading, time, traceback
from dataclasses import dataclass, field
from pathlib import Path
from jinja2 import StrictUndefined, Template
from aios import __version__
from aios.env import Submitted
from aios.model import FormatError
from aios.abi import ABI
from aios import middleware as mw_mod

log = logging.getLogger("aios.agent")


@dataclass
class AgentConfig:
    system_template: str
    instance_template: str
    step_limit: int = 0
    cost_limit: float = 3.0
    wall_time_limit_seconds: int = 0
    max_consecutive_format_errors: int = 3
    output_path: str | None = None
    middleware: list = field(default_factory=list)     # aios/middleware.py: names or {"name", options}; the hooks of the loop
    harness_home: str = ""                             # aios/home.py: the person's harness directory this run used, and its digest; recorded in the trajectory so an acceptance can name the exact triple (sandboxd, home, aios)
    harness_digest: str = ""
    progress_nudge_at: int = 0     # kept for old configs: the same as {"name": "progress_nudge", "at": N} in `middleware`. Off by default. At this model call, if no file in the working tree has changed, say so once: in the 11 baseline runs on the user's own tasks two never wrote a file in 30 steps and five first wrote at step 16 or later (docs/history/d17, D22)


class Agent:
    def __init__(self, model, env, **kw):
        self.config = AgentConfig(**kw); self.model = model; self.env = env
        self.messages = []; self.extra_template_vars = {}; self.cost = 0.0; self.n_calls = 0; self.n_fmt = 0; self._t0 = time.time()
        specs = list(self.config.middleware)
        if self.config.progress_nudge_at and not any((s.get("name") if isinstance(s, dict) else s) == "progress_nudge" for s in specs): specs.append({"name": "progress_nudge", "at": self.config.progress_nudge_at})
        self.mw = mw_mod.load(specs)
        self.abi = ABI()                              # kernel ABI: events + budget go to agent-runtime when it is there
        self.inbox_n = 0                              # last Input read from the kernel inbox (ticket mode)
        self.interrupted = 0; self._stragglers = []   # model calls dropped because a person spoke (001 第 6 阶段); their cost is charged when they finish

    def template_vars(self, **kw) -> dict:
        return {**self.config.__dict__, **self.env.get_template_vars(), **self.model.get_template_vars(),
                "n_model_calls": self.n_calls, "model_cost": self.cost, "elapsed_seconds": int(time.time() - self._t0), **self.extra_template_vars, **kw}

    def render(self, tpl: str) -> str: return Template(tpl, undefined=StrictUndefined).render(**self.template_vars())

    def add(self, *msgs):
        for m in msgs: log.debug("%s", m)
        self.messages.extend(msgs); return list(msgs)

    def exit(self, status, submission="", **extra):
        if self.abi.available: self.abi.event("exit", status=status, calls=self.n_calls, cost=self.cost)
        self.add({"role": "exit", "content": status if not submission else submission, "extra": {"exit_status": status, "submission": submission, **extra}})

    def run(self, task: str, **kw) -> dict:
        self.extra_template_vars |= {"task": task, **kw}; self.messages = []
        self.add(self.model.format_message(role="system", content=self.render(self.config.system_template)),
                 self.model.format_message(role="user", content=self.render(self.config.instance_template)))
        while True:
            try:
                self.step(); self.n_fmt = 0
            except FormatError as e:
                self.cost += e.message.get("extra", {}).get("cost", 0.0); self.n_fmt += 1; self.add(e.message)
                if 0 < self.config.max_consecutive_format_errors <= self.n_fmt: self.exit("RepeatedFormatError")
            except Submitted as e: self.exit("Submitted", e.submission)
            except _Limit as e: self.exit(e.status)
            except Exception as e:  # noqa: BLE001
                self.exit(type(e).__name__, exception_str=str(e), traceback=traceback.format_exc()); self.save(); raise
            finally: self.save()
            if self.messages[-1].get("role") == "exit": break
        return self.messages[-1].get("extra", {})

    def step(self):
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost: raise _Limit("LimitsExceeded")
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._t0): raise _Limit("TimeExceeded")
        self.n_calls += 1
        self._collect_stragglers(); self.read_inbox()
        for m in self.mw: m.before_step(self)
        msg = self._query_interruptible(); ex = msg.get("extra", {}); self.cost += ex.get("cost", 0.0); self.add(msg)
        if self.abi.available:
            r = self.abi.charge(ex.get("tokens_in", 0), ex.get("tokens_out", 0), ex.get("cost", 0.0))
            self.abi.event("model_call", calls=self.n_calls, tokens_in=ex.get("tokens_in", 0), tokens_out=ex.get("tokens_out", 0), tokens_cached=ex.get("tokens_cached", 0), cost=ex.get("cost", 0.0),
                           actions=[a.get("command", "")[:200] for a in ex.get("actions", [])])
            if r and r.get("over"): raise _Limit("BudgetExceeded")
        outputs = []
        try:
            for a in msg.get("extra", {}).get("actions", []): outputs.append(self.env.execute(a))
        finally:   # on Submitted the observations so far are still recorded
            self.add(*self.model.format_observation_messages(msg, outputs, self.template_vars()))
            for m in self.mw: m.after_step(self, msg, outputs)

    def _view(self):
        """What the model is sent: the conversation, passed through the middleware's before_query. A copy; the recorded trajectory is untouched."""
        msgs = list(self.messages)
        for m in self.mw: msgs = m.before_query(self, msgs)
        return msgs

    def _query_interruptible(self):
        """The model call runs in a thread; meanwhile the inbox is read every second. A person's words drop the call in flight
        and the step re-plans with them in the context (docs/001 第 6 阶段, docs/006: 插话优先于排着的工作)."""
        if not self.abi.available: return self.model.query(self._view())
        for _ in range(8):                                                         # a burst of inputs re-plans at most this often per step
            result, done = {}, threading.Event(); snapshot = self._view()
            def run(result=result, done=done, snapshot=snapshot):
                try: result["msg"] = self.model.query(snapshot)
                except BaseException as e: result["err"] = e  # noqa: BLE001
                finally: done.set()
            threading.Thread(target=run, daemon=True).start()
            while not done.wait(1.0):
                if self.read_inbox(human_only=True):
                    self.interrupted += 1; self._stragglers.append((done, result))
                    self.abi.event("interrupted", calls=self.n_calls, by="person"); break
            else:
                if "err" in result: raise result["err"]
                return result["msg"]
        raise RuntimeError("interrupted repeatedly")

    def _collect_stragglers(self):
        """Dropped calls still cost money: when they finish, charge them like any other call."""
        keep = []
        for done, result in self._stragglers:
            if not done.is_set(): keep.append((done, result)); continue
            ex = (result.get("msg") or {}).get("extra", {}) if "msg" in result else {}
            if ex.get("cost"): self.cost += ex["cost"]; self.abi.charge(ex.get("tokens_in", 0), ex.get("tokens_out", 0), ex["cost"])
        self._stragglers = keep

    def read_inbox(self, human_only=False):
        """Inputs the host delivered since the last step (a person's follow-up, a policy change) become user messages. Returns how many."""
        if not self.abi.available: return 0
        n = 0
        for inp in self.abi.inbox(self.inbox_n):
            if human_only and not (inp.get("human") or inp.get("kind") == "answer"): continue
            self.inbox_n = max(self.inbox_n, int(inp.get("n", 0)))
            who = inp.get("actor") or inp.get("kind", "input")
            self.add(self.model.format_message(role="user", content=f"<input kind=\"{inp.get('kind', 'text')}\" from=\"{who}\">\n{inp.get('text', '')}\n</input>")); n += 1
        return n

    def serialize(self) -> dict:
        last = self.messages[-1].get("extra", {}) if self.messages else {}
        d = {"info": {"model_stats": {"instance_cost": self.cost, "api_calls": self.n_calls},
                      "config": {"agent": {k: v for k, v in self.config.__dict__.items() if k not in ("system_template", "instance_template")}, "agent_type": "aios.agent.Agent"},
                      "cairn_version": __version__, "exit_status": last.get("exit_status", ""), "submission": last.get("submission", "")},
             "messages": self.messages, "trajectory_format": "mini-swe-agent-1.1"}
        for part in (self.model.serialize(), self.env.serialize()):
            for k, v in part["info"].items():
                if isinstance(v, dict) and isinstance(d["info"].get(k), dict): d["info"][k].update(v)
                else: d["info"][k] = v
        return d

    def save(self):
        if self.config.output_path:
            p = Path(self.config.output_path); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.serialize(), indent=1, default=str))


class _Limit(Exception):
    def __init__(self, status): self.status = status; super().__init__(status)
