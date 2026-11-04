"""docs/001 第 6 阶段 / docs/006: a person's words arriving during a model call drop that call and re-plan with them in the context."""
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from aios.agent import Agent


class SlowModel:
    """First query blocks until released (as if the model were still generating); later ones answer at once, with no actions."""
    def __init__(self): self.release = threading.Event(); self.calls = []; self.stats = {}
    def query(self, messages):
        self.calls.append([m["content"] for m in messages if m["role"] == "user"][-1])
        if len(self.calls) == 1: self.release.wait(10); return {"role": "assistant", "content": "late", "extra": {"actions": [], "cost": 0.5}}
        return {"role": "assistant", "content": "replanned", "extra": {"actions": [], "cost": 0.1}}
    def format_message(self, **kw): return kw
    def format_observation_messages(self, message, outputs, template_vars=None): return [{"role": "user", "content": "obs"}]
    def get_template_vars(self): return {}
    def serialize(self): return {"info": {}}


class FakeABI:
    """The kernel inbox: one person's answer appears shortly after the first poll."""
    def __init__(self): self.available = True; self.t0 = time.time(); self.events = []; self.charged = []
    def inbox(self, since=0):
        items = [{"n": 1, "kind": "answer", "actor": "me", "text": "不对,先别改那个文件"}] if time.time() - self.t0 > 0.3 else []
        return [i for i in items if i["n"] > since]
    def event(self, type_, **data): self.events.append((type_, data))
    def charge(self, *a): self.charged.append(a); return {"over": False}


class Env:
    def execute(self, a): return {}
    def get_template_vars(self): return {}
    def serialize(self): return {"info": {}}


def test_a_persons_answer_interrupts_the_model_call_and_replans():
    m = SlowModel(); a = Agent(m, Env(), system_template="s", instance_template="t"); a.abi = FakeABI()
    a.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    t0 = time.time(); a.step(); dt = time.time() - t0
    assert dt < 5, "the step waited for the slow call instead of dropping it"
    assert a.interrupted == 1 and m.calls[1].startswith("<input kind=\"answer\" from=\"me\">") and "先别改" in m.calls[1]
    assert a.messages[-2]["content"] == "replanned" and a.cost == 0.1                   # the dropped call's cost is not charged yet
    assert ("interrupted", {"calls": 1, "by": "person"}) in a.abi.events
    m.release.set(); time.sleep(0.2); a._collect_stragglers()
    assert abs(a.cost - 0.6) < 1e-9 and a.abi.charged                                    # ... but it is charged once it finishes
    # without a person's input the call runs to completion
    m2 = SlowModel(); m2.release.set(); b = Agent(m2, Env(), system_template="s", instance_template="t"); b.abi = FakeABI(); b.abi.t0 = time.time() + 100
    b.messages = [{"role": "user", "content": "task"}]; b.step(); assert b.interrupted == 0 and b.messages[-2]["content"] == "late"


if __name__ == "__main__":
    test_a_persons_answer_interrupts_the_model_call_and_replans(); print("agent interrupt ok")


class NudgeEnv(Env):
    def __init__(self, changed): self.changed = changed; self.ran = []
    def execute(self, a): self.ran.append(a["command"]); return {"output": "./pkg/a.py\n" if self.changed and "find ." in a["command"] else "", "returncode": 0}


class Quiet(SlowModel):
    def __init__(self): super().__init__(); self.release.set(); self.seen = []
    def query(self, messages): self.seen.append(list(messages)); return {"role": "assistant", "content": "x", "extra": {"actions": [], "cost": 0.0}}


def _run_to(n, changed, **kw):
    m = Quiet(); env = NudgeEnv(changed); a = Agent(m, env, system_template="s", instance_template="t", step_limit=60, **kw)
    a.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for _ in range(n): a.step()
    return a, m, env


def test_an_agent_that_has_changed_nothing_by_the_marked_step_is_told_once_and_one_that_has_is_left_alone():
    a, m, env = _run_to(6, False, middleware=[{"name": "progress_nudge", "at": 3}])
    nudges = [x for x in a.messages if x["role"] == "user" and "progress check" in x["content"]]
    assert len(nudges) == 1 and "step 3 of 60" in nudges[0]["content"] and "touch /tmp/.cairn_start" in env.ran[0]
    a, m, env = _run_to(6, True, middleware=[{"name": "progress_nudge", "at": 3}])
    assert not [x for x in a.messages if "progress check" in str(x.get("content"))]


def test_the_old_config_key_still_turns_the_nudge_on_and_nothing_runs_when_it_is_off():
    a, m, env = _run_to(4, False, progress_nudge_at=2)
    assert [x for x in a.messages if "progress check" in str(x.get("content"))]
    a, m, env = _run_to(6, False)
    assert not env.ran and not [x for x in a.messages if "progress check" in str(x.get("content"))]


def test_a_missing_start_marker_says_nothing_about_progress():
    class NoMark(NudgeEnv):
        def execute(self, a): self.ran.append(a["command"]); return {"output": "NOMARK\n", "returncode": 0}
    m = Quiet(); env = NoMark(False); a = Agent(m, env, system_template="s", instance_template="t", middleware=[{"name": "progress_nudge", "at": 2}])
    a.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for _ in range(3): a.step()
    assert not [x for x in a.messages if "progress check" in str(x.get("content"))]


def test_before_query_changes_what_the_model_is_sent_and_not_what_is_recorded():
    big = "x" * 5000
    m = Quiet(); a = Agent(m, Env(), system_template="s", instance_template="t", middleware=[{"name": "compact_history", "keep": 2, "limit": 100, "batch": 1}])
    a.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}, {"role": "assistant", "content": "c"}, {"role": "tool", "content": big}] + [{"role": "user", "content": "k"}] * 4
    a.step()
    sent = m.seen[0]
    assert any("left out" in str(x.get("content")) for x in sent) and a.messages[3]["content"] == big                  # shortened in what was sent, whole in the record


def test_an_unknown_middleware_is_an_error_not_a_silent_no_op():
    import pytest
    with pytest.raises(ValueError):
        Agent(Quiet(), Env(), system_template="s", instance_template="t", middleware=["no_such_thing"])
    import aios.middleware as M
    class Mine(M.Middleware):
        def __init__(self, n=1): self.n = n
    import sys, types
    mod = types.ModuleType("cairn_test_mw"); mod.Mine = Mine; sys.modules["cairn_test_mw"] = mod
    a = Agent(Quiet(), Env(), system_template="s", instance_template="t", middleware=[{"class": "cairn_test_mw:Mine", "n": 4}])
    assert a.mw[0].n == 4                                                                                          # a class a candidate wrote, with its options
