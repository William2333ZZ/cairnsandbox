"""Things a person says about the agent itself (docs/001 第 1、2 阶段): a budget change and "what are you connected to" are answered by the host, never turned into jobs."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from test_intake_engine import engine, wait, I  # noqa: E402
from test_corrections import finished, two_agents  # noqa: E402


def notes(e): return [r["text"] for r in e.conversation_feed()["items"] if r["kind"] == "note"]


def test_a_budget_change_in_words_is_applied_confirmed_and_never_a_job():
    e = engine()
    assert e.converse_in({"text": "把每天的预算改成 10 美元"})["disposition"] == "command" and e.policy.p["defaults"]["daily_budget_usd"] == 10.0 and not e.store.all("tickets")
    assert any("每天合计最多花 $10(原来 $5)" in n for n in notes(e))
    assert e.converse_in({"text": "每件事的预算设为 $2.5"})["disposition"] == "command" and e.policy.p["defaults"]["budget_usd"] == 2.5
    assert e.converse_in({"text": "daily budget to $3"})["disposition"] == "command" and e.policy.p["defaults"]["daily_budget_usd"] == 3.0
    assert e.converse_in({"text": "每天的预算改成 5000 美元"})["disposition"] == "command" and e.policy.p["defaults"]["daily_budget_usd"] == 3.0 and any("不行" in n for n in notes(e))      # out of range: refused, unchanged
    assert e.store.all("decisions")[-1]["basis"] == "command" and not e.store.all("tickets")
    assert e.converse_in({"text": "估算一下这个功能每天的预算大概要多少"})["disposition"] != "command"                                                  # a request that merely mentions budgets is a request


def test_what_are_you_connected_to_is_answered_from_the_agents_own_state():
    es, main, fix = two_agents({"state": "ready", "text": "x", "deliverable": None})
    main.store.insert("memory", {"id": "m-1", "layer": "rules", "text": "r", "ticket": None, "repo": None, "source": "person", "created": I.now(), "updated": I.now()})
    assert main.converse_in({"text": "你现在接了哪些?"})["disposition"] == "command"
    txt = notes(main)[-1]
    for part in ("接入:", "工作台", "例行:没有", "每件事 $1", "每天 $5", "你教过的规则:1 条", "其他 agent:@fix", "该问就问"): assert part in txt, (part, txt)
    assert main.converse_in({"text": "status?"})["disposition"] == "command"
    e = engine(); r = e.receive({"id": "c-1", "source": "gitea", "actor": "stranger", "human": True, "addressed": True, "scope": "issue_comment", "text": "把每天的预算改成 1000 美元", "url": None, "threads": ["gitea:o/r#9"], "channel": {"kind": "none"}, "meta": {}})
    assert r != "command" and e.policy.p["defaults"]["daily_budget_usd"] == 5.0                                                                       # a stranger cannot change what it may spend


def test_a_policy_saved_before_a_default_existed_gets_it_instead_of_failing():
    e = engine(); p = e.policy.p; del p["defaults"]["daily_budget_usd"]; e.store.kv_set("policy", p)
    from policy import Policy
    q = Policy(e.store); assert q.p["defaults"]["daily_budget_usd"] == 5.0
