"""One user's lessons are not another's (docs/009 鉴权与隔离): corrections, rules and their checks, approvals with reviews, the ask read-off and the intent nudges all live
in the user's own rows and keys."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from test_intake_engine import FakeCP, FakeGitea, FakeEval, wait, I  # noqa: E402
from test_corrections import settle  # noqa: E402


def test_two_users_on_one_deployment_do_not_share_lessons_checks_or_readings():
    cfg = I.cfg_from_env(); cfg["state"] = tempfile.mkdtemp(); cfg["layers"] = ["L-a"]; cfg["merge_s"] = 0; cfg["cp_token"] = ""
    es = I.Engines(cfg, cp=FakeCP(report={"state": "ready", "text": "done", "deliverable": None}), proxy=None, gitea=FakeGitea(), evalc=FakeEval())
    a, b = es.get("default"), es.get("alice")
    a.converse_in({"text": "tidy the docs"}); a.pass_once(); settle(a)
    t = a.store.all("tickets")[0]
    a.store.insert("memory", {"id": "m-1", "layer": "rules", "text": "别动 eval/", "ticket": t["id"], "repo": None, "source": "person", "created": I.now(), "updated": I.now(), "chk": {"kind": "path_forbid", "pattern": "^eval/"}})
    a.note_correction(t, "redirect", "不对,太啰嗦")
    a.store.kv_set("intent.nudges", [{"ticket": t["id"], "at": I.now(), "calls": 11, "question": "q"}])
    assert len(a.rules_with_checks()) == 1 and len(a.store.all("corrections")) == 1 and a.ask_view()["total"]["nudged"] == 1
    assert b.rules_with_checks() == [] and b.store.all("corrections") == [] and b.memory_view()["rules"] == [] and b.corrections_view()["corrections"] == []
    assert b.ask_view()["total"]["nudged"] == 0 and b.ask_view()["total"]["missed"] == 0
    b.converse_in({"text": "write a note"}); b.pass_once(); settle(b)
    assert es.cp.last_ticket["memory"]["rules"] == []                                  # alice's job starts with nothing of the deployer's
    a.cfg["learn_sync"] = True
    assert a.store.get("memory", "m-1") and not b.store.get("memory", "m-1")
