"""dsh-superagent 001/003: a PR belongs to the Ticket that opened it, a Thread can be followed for context only, and Threads that share a Trigger are correlated."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from test_intake_engine import engine, wait, I  # noqa: E402
from test_review import pr_job  # noqa: E402
from test_corrections import DELIVER  # noqa: E402

PR_THREAD = "gitea:o/r#7"


def trig(i, text, threads, human=True, actor="alice"):
    return {"id": i, "source": "gitea", "actor": actor, "human": human, "addressed": True, "scope": "pr_comment", "text": text, "url": None, "threads": threads, "channel": None, "meta": {}}


def the_ticket(e): return e.store.all("tickets")[0]


def test_the_pr_a_ticket_opens_is_followed_by_that_ticket_and_a_comment_there_continues_the_work():
    e, ap = pr_job([{"filename": "pkg/a.py", "status": "modified", "additions": 1, "deletions": 1}])
    t = the_ticket(e); assert t["pr"]["number"] == 7
    assert t["id"] in (e.store.get("threads", PR_THREAD) or {}).get("subscriptions", [])
    assert e.receive(trig("c1", "please also handle the empty case", [PR_THREAD])) == "fanned-out"          # the Ticket's, not a fresh judgement
    assert any(i.get("trigger") == "c1" and not i.get("context") for i in the_ticket(e)["inputs"])
    assert any(f["thread"] == PR_THREAD and f["mode"] == "own" for f in e.ticket_view(t["id"])["follows"])


def test_a_context_thread_delivers_messages_without_making_them_the_tickets_to_answer():
    e, ap = pr_job([{"filename": "pkg/a.py", "status": "modified", "additions": 1, "deletions": 1}])
    t = the_ticket(e); other = "gitea:o/r#9"
    r = e.subscribe(t["id"], other, "context"); assert r["mode"] == "context"
    assert e.subscribe(t["id"], other, "context")["mode"] == "context"                                             # idempotent
    out = e.receive(trig("c2", "this issue is related: the loader also breaks on empty input", [other]))
    ins = [i for i in the_ticket(e)["inputs"] if i.get("trigger") == "c2"]
    assert ins and ins[0]["context"] is True and ins[0]["human"] is False                                          # read, but not a person's word to this ticket
    assert out != "fanned-out"                                                                                     # nobody owns it: Eval sees it
    e.subscribe(t["id"], other, "own"); th = e.store.get("threads", other)
    assert t["id"] in th["subscriptions"] and t["id"] not in th["context"]                                         # reading became owning


def test_a_context_thread_never_wakes_an_ended_ticket():
    e, ap = pr_job([{"filename": "pkg/a.py", "status": "modified", "additions": 1, "deletions": 1}])
    t = the_ticket(e); other = "gitea:o/r#9"; e.subscribe(t["id"], other, "context")
    e.store.update("tickets", t["id"], {"state": "ended", "ended_by": "pr-closed"})
    e.receive(trig("c3", "any news?", [other]))
    assert e.store.get("tickets", t["id"])["state"] == "ended" and not [i for i in e.store.get("tickets", t["id"])["inputs"] if i.get("trigger") == "c3"]


def test_threads_that_share_a_trigger_are_correlated_and_eval_sees_what_the_other_one_said():
    e = engine()
    e.receive(trig("a1", "the loader crashes on empty files", ["gitea:o/r#1"], actor="alice"))
    e.receive(trig("a2", "see also the discussion on the other issue", ["gitea:o/r#2", "gitea:o/r#1"], actor="bob"))
    assert "gitea:o/r#1" in e.store.get("threads", "gitea:o/r#2")["correlated"] and "gitea:o/r#2" in e.store.get("threads", "gitea:o/r#1")["correlated"]
    rel = e._related_text(e.store.get("threads", "gitea:o/r#2"))
    assert "Related conversations" in rel and "crashes on empty files" in rel
    assert e._related_text(e.store.get("threads", "gitea:o/r#99") or {"correlated": []}) == ""


def test_following_an_unknown_ticket_or_with_a_bad_mode_is_refused():
    e = engine()
    for args in (("t-nope", "gitea:o/r#1", "own"),):
        try: e.subscribe(*args); raise AssertionError("accepted")
        except KeyError: pass
    e2, _ = pr_job([{"filename": "pkg/a.py", "status": "modified", "additions": 1, "deletions": 1}])
    try: e2.subscribe(the_ticket(e2)["id"], "gitea:o/r#3", "sideways"); raise AssertionError("accepted")
    except ValueError: pass


def test_a_ticket_that_is_stuck_gets_no_automatic_input_and_one_that_is_not_does():
    e, ap = pr_job([{"filename": "pkg/a.py", "status": "modified", "additions": 1, "deletions": 1}])
    t = the_ticket(e)
    e.store.update("tickets", t["id"], {"last_report": {"state": "failed", "text": "x"}})
    e.receive(trig("ci1", "CI failed on main", [PR_THREAD], human=False, actor="ci-bot"))
    held = [i for i in the_ticket(e)["inputs"] if i.get("trigger") == "ci1"]
    assert held and held[0].get("held") is True and held[0]["delivered"] is False                                  # recorded, not delivered
    e.store.update("tickets", t["id"], {"last_report": {"state": "ready", "text": "x"}})
    e.receive(trig("ci2", "CI passed", [PR_THREAD], human=False, actor="ci-bot"))
    assert not [i for i in the_ticket(e)["inputs"] if i.get("trigger") == "ci2"][0].get("held")


def test_a_message_a_ticket_never_took_in_goes_back_to_eval_when_the_ticket_ends():
    e, ap = pr_job([{"filename": "pkg/a.py", "status": "modified", "additions": 1, "deletions": 1}])
    t = the_ticket(e); e.receive(trig("h1", "and one more thing", [PR_THREAD]))
    inputs = the_ticket(e)["inputs"]; assert inputs and inputs[-1]["trigger"] == "h1"
    for i in inputs:
        if i["trigger"] == "h1": i["delivered"] = False
    e.store.update("tickets", t["id"], {"inputs": inputs, "state": "ended", "ended_by": "executor", "outcome": "ready"})
    assert e._return_unread(t["id"]) == 1 and e._return_unread(t["id"]) == 0                                       # once
    ev = e.store.get("threads", PR_THREAD)["eval"]; assert "h1" in ev["triggers"] and e.store.get("triggers", "h1")["disposition"] == "returned-to-eval"
    e.store.update("tickets", t["id"], {"ended_by": "person"}); i2 = the_ticket(e)["inputs"]; i2[-1]["returned"] = False; e.store.update("tickets", t["id"], {"inputs": i2})
    assert e._return_unread(t["id"]) == 0                                                                          # a person stopped it: their stop stands


def test_messages_to_one_conversation_go_out_in_order_even_when_the_first_one_fails():
    import time as _t
    e = engine(); sent = []
    def comment(n, text):
        if text == "first" and not sent: sent.append("boom"); raise RuntimeError("down")
        sent.append(text)
    e.gitea.comment = comment
    for k, txt in enumerate(("first", "second")):
        e.store.insert("outbox", {"id": f"m{k}", "channel": {"kind": "gitea", "issue": 3}, "ticket": "t", "purpose": "ack", "text": txt, "state": "queued", "attempts": 0, "error": None, "created": _t.time() + k})
    e.flush_outbox()
    assert sent == ["boom"] and e.store.get("outbox", "m1")["state"] == "queued"                                   # "second" waits for "first"
    e.store.update("outbox", "m0", {"sent": _t.time() - 600}); e.flush_outbox()
    assert sent == ["boom", "first", "second"]
