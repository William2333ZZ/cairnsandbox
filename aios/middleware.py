"""Middleware: hooks in the tool loop (docs/004 harness 的 8 个组件, AHE's seven editable parts). One class per behaviour, switched on by name in config.json:

    "agent": {"middleware": ["compact_history", {"name": "progress_nudge", "at": 15}]}

A candidate harness adds or removes one entry (or one file defining a class) instead of editing the loop, so a change is small, can be attributed on its own and is rolled back by
removing a line. Three hooks:
  before_step(agent)            at the start of a model call, after the inbox is read
  before_query(agent, messages) the list the model is sent; return a new list (never change agent.messages: the trajectory records what happened)
  after_step(agent, msg, outputs) after the observations are added

One invariant every hook that touches `before_query` must keep: the provider caches the longest identical prefix of what is sent, and a cached input token costs 1/50 of a fresh
one (docs/decisions D22). Rewriting the early part of the conversation at every step (a sliding window) makes the whole conversation fresh again; change it in steps (`batch`), or not at all."""
import importlib


class Middleware:
    name = ""
    def before_step(self, agent): pass
    def before_query(self, agent, messages): return messages
    def after_step(self, agent, msg, outputs): pass


class CompactHistory(Middleware):
    """Old command outputs shrink to head and tail in what is sent (aios.model.compact_history). `batch` > 1 keeps the sent prefix identical for that many messages."""
    name = "compact_history"
    def __init__(self, keep=8, limit=1500, batch=8): self.keep, self.limit, self.batch = keep, limit, batch
    def before_query(self, agent, messages):
        from aios.model import compact_history
        return compact_history(messages, keep=self.keep, limit=self.limit, batch=self.batch)


class ProgressNudge(Middleware):
    """Once, at model call `at`: if no file in the working tree has changed since the start, say how far in the agent is. Exploring is not progress; in the first baseline on the user's own
    tasks two of eleven runs read for 30 steps and never wrote a file, and five first wrote at step 16 or later."""
    name = "progress_nudge"
    MARK = "/tmp/.cairn_start"
    def __init__(self, at=15): self.at, self.done, self.marked = int(at), False, False

    def _mark(self, agent):
        self.marked = True
        try: agent.env.execute({"command": f"touch {self.MARK}"})
        except Exception: pass                                                     # noqa: BLE001 - a nudge is a courtesy; it never stops a run

    def before_step(self, agent):
        if not self.marked: self._mark(agent)
        if not self.at or self.done or agent.n_calls != self.at: return
        self.done = True
        try:
            out = agent.env.execute({"command": f"[ -f {self.MARK} ] || echo NOMARK; find . \\( -path ./.git -o -path ./.task -o -name __pycache__ -o -name .pytest_cache -o -name node_modules \\) -prune -o -type f -newer {self.MARK} -print 2>/dev/null | head -1"})
            if str(out.get("output", "")).strip(): return                          # a changed file, or NOMARK (no start marker: nothing can be said about progress)
        except Exception: return                                                   # noqa: BLE001
        limit = agent.config.step_limit
        agent.add(agent.model.format_message(role="user", content=f"[progress check] This is step {self.at}" + (f" of {limit}" if limit else "") + ": no file in the working tree has changed yet. "
                  "Stop exploring. Pick the most likely place, make a first small change and run the checks; refine from there. "
                  "If you cannot go on because of a decision only the person can make, say so now instead of reading more."))
        if agent.abi.available: agent.abi.event("progress_nudge", calls=self.at)


BUILTIN = {"compact_history": CompactHistory, "progress_nudge": ProgressNudge}


def load(specs):
    """specs: names, {"name": ..., options}, or {"class": "package.module:ClassName", options} for a class a candidate wrote. A spec that cannot be loaded raises: a harness with a
    broken middleware list must fail its self-check, not run without the behaviour it claims."""
    out = []
    for s in specs or []:
        opts = dict(s) if isinstance(s, dict) else {"name": s}
        ref = opts.pop("class", None); name = opts.pop("name", None)
        if ref:
            mod, _, cls = ref.partition(":"); factory = getattr(importlib.import_module(mod), cls)
        elif name in BUILTIN: factory = BUILTIN[name]
        else: raise ValueError(f"unknown middleware {name!r}; known: {', '.join(sorted(BUILTIN))}")
        out.append(factory(**opts))
    return out
