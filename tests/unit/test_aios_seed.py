"""Cairn stage-0 seed: loop, tool-call parsing, submit convention, limits (no network: fake model)."""
import json, os, sys, threading, time, urllib.request
from http.server import ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
from aios.agent import Agent
from aios.env import LocalEnvironment, Submitted
from aios.model import Model, FormatError

CFG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "aios", "config.json")))


class FakeModel(Model):
    def __init__(self, script, **kw):
        super().__init__(model_name="fake", base_url="http://x", **kw); self.script = list(script)
    def _post(self, payload):
        n = sum(1 for m in payload["messages"] if m.get("role") == "assistant"); item = self.script[n] if n < len(self.script) else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        tc = [] if item is None else [{"id": f"c{n}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": item})}}]
        return {"choices": [{"message": {"role": "assistant", "content": "thinking", "tool_calls": tc}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}, {"x-cost-usd": "0.001", "x-tokens-in": "10", "x-tokens-out": "2"}


def agent(tmp, script, **over):
    cfg = {**CFG["agent"], "output_path": str(tmp / "traj.json"), **over}
    return Agent(FakeModel(script, **CFG["model"]), LocalEnvironment(cwd=str(tmp), **CFG["environment"]), **cfg)


def test_loop_runs_commands_and_submits(tmp_path):
    a = agent(tmp_path, ["echo hi > out.txt", "cat out.txt"])
    r = a.run("make out.txt")
    assert r["exit_status"] == "Submitted" and (tmp_path / "out.txt").read_text() == "hi\n"
    t = json.loads((tmp_path / "traj.json").read_text())
    assert t["trajectory_format"] == "mini-swe-agent-1.1" and t["info"]["exit_status"] == "Submitted" and t["info"]["model_stats"]["api_calls"] == 3
    assert abs(a.cost - 0.003) < 1e-9                      # cost comes from the proxy headers
    roles = [m["role"] for m in t["messages"]]; assert roles[:2] == ["system", "user"] and roles[-1] == "exit" and "tool" in roles


def test_format_errors_then_exit(tmp_path):
    a = agent(tmp_path, [None, None, None, "ls"])
    r = a.run("x"); assert r["exit_status"] == "RepeatedFormatError" and a.n_fmt == 3


def test_step_limit(tmp_path):
    a = agent(tmp_path, ["true"] * 10, step_limit=2)
    assert a.run("x")["exit_status"] == "LimitsExceeded" and a.n_calls == 2


def test_env_timeout_and_submit(tmp_path):
    env = LocalEnvironment(cwd=str(tmp_path), timeout=1)
    o = env.execute({"command": "sleep 5"}); assert o["returncode"] == -1 and "TimeoutExpired" in o["extra"]["exception_type"]
    try: env.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; echo done"}); assert False
    except Submitted as s: assert s.submission.strip() == "done"
    o = env.execute({"command": "echo not COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}); assert o["returncode"] == 0   # marker must be the first line


def test_parse_actions_rejects_unknown_tool():
    m = Model(model_name="x", base_url="http://x", format_error_template="{{ error }}")
    try: m._parse_actions([{"id": "1", "function": {"name": "python", "arguments": "{}"}}], "stop"); assert False
    except FormatError as e: assert "Unknown tool" in e.message["content"]


def test_model_proxy_meters_and_refuses(tmp_path):
    os.environ["PROXY_STATE"] = str(tmp_path); os.environ["MODEL_BACKEND"] = "mock"
    import importlib, modelproxy; importlib.reload(modelproxy)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), modelproxy.H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    def post(path, body, tok=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read()), {}
    st, t, _ = post("/tokens", {"sandbox": "sb1", "budget_tokens": 400}); assert st == 201
    st, resp, hdr = post("/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]}, t["token"])
    assert st == 200 and resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash" and "X-Tokens-In" in hdr
    for _ in range(50):
        st, resp, _ = post("/v1/chat/completions", {"messages": [{"role": "user", "content": "x" * 400}]}, t["token"])
        if st == 402: break
    assert st == 402, "budget must be enforced"
    assert post("/v1/chat/completions", {"messages": []}, "bad")[0] == 401
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/usage/sb1") as r: u = json.loads(r.read())
    assert u["refused"] >= 1 and u["calls"] >= 1 and u["tokens_in"] > 0
    srv.shutdown()


def test_old_command_output_shrinks_in_what_is_sent_but_the_task_the_calls_and_the_latest_steps_stay():
    from aios.model import compact_history
    big = "x" * 9000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "the task " + big}]
    for i in range(12): msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": "bash", "arguments": "{\"command\": \"cat f\"}"}}]}, {"role": "tool", "tool_call_id": f"c{i}", "content": f"out{i} " + big}]
    out = compact_history(msgs)
    assert len(out) == len(msgs) and out[1]["content"] == msgs[1]["content"] and out[2] == msgs[2]                      # the task and the calls are as they were
    assert len(out[3]["content"]) < 1800 and "left out to keep the conversation short" in out[3]["content"] and out[3]["content"].startswith("out0 ")
    assert out[-1] == msgs[-1] and out[-2] == msgs[-2] and out[-7] == msgs[-7]                                          # the latest eight messages are whole
    assert sum(len(m.get("content") or "") for m in out) < sum(len(m.get("content") or "") for m in msgs) / 2
    assert msgs[3]["content"].startswith("out0 ") and len(msgs[3]["content"]) > 9000                                    # the input list is not changed


def test_compaction_is_off_unless_a_version_turns_it_on():
    from aios.model import Model, ModelConfig
    import inspect
    assert ModelConfig.__dataclass_fields__["compact_history"].default is False and "compact_history(prepared" in inspect.getsource(Model.query)


def test_a_batched_boundary_keeps_the_sent_prefix_unchanged_between_steps_so_a_prefix_cache_keeps_hitting():
    from aios.model import compact_history
    big = "y" * 6000
    def convo(n):
        m = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
        for i in range(n): m += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]}, {"role": "tool", "tool_call_id": f"c{i}", "content": f"o{i} " + big}]
        return m
    def sent(n, batch): return compact_history(convo(n), batch=batch)
    # sliding (batch 1): the compacted prefix of the next step differs from this step's (an older message shrank)
    a, b = sent(20, 1), sent(21, 1); assert any(x != y for x, y in zip(a, b))
    # batched: within a batch of 12 messages the first messages are byte-identical from one step to the next
    shrunk = lambda msgs: [m["content"] for m in msgs if m.get("role") == "tool" and "left out" in m["content"]]
    a, b, c = sent(21, 12), sent(22, 12), sent(23, 12); assert shrunk(a) == shrunk(b) == shrunk(c) and shrunk(a)
    assert shrunk(sent(27, 12)) != shrunk(a)                                                                              # the boundary moves when the batch is full


def test_the_harnesss_own_notes_are_added_to_the_task_only_when_there_is_something_in_them():
    import tempfile, os
    from aios import run as R
    d = tempfile.mkdtemp(); p = os.path.join(d, "memory.md")
    assert R.harness_memory(p) == ""                                                                              # no file
    open(p, "w").write("<!-- for people -->\n"); assert R.harness_memory(p) == ""                               # only a comment
    open(p, "w").write("<!-- c -->\nRun the narrowest test first.\n")
    out = R.harness_memory(p); assert "Run the narrowest test first." in out and "for people" not in out and "the request above wins" in out
    seed = os.path.join(os.path.dirname(R.__file__), "memory.md"); assert R.harness_memory(seed) == ""           # the seed ships empty
