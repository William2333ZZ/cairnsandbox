"""Intake engine (docs/005, 005) against a fake control plane: gate, accept, assignment, report, resume, stop, policy versions."""
import json, os, sys, time, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "intake"))
import intake as I  # noqa: E402


class FakeCP:
    last_ticket = {}
    patch = b"From 0 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n"                          # bytes, or a function of the last ticket
    def __init__(self, report=None):
        self.n = 0; self.calls = []; self.inbox_items = []; self.report = report or {"state": "ready", "text": "done"}
    def _req(self, *a, **k): return {"ok": True}
    def create(self, spec): self.n += 1; self.calls.append(("create", spec)); return f"sb{self.n}", "fc-node"
    def put(self, sid, path, data): self.calls.append(("put", path))
    def get(self, sid, path): return self.patch(self.last_ticket) if callable(self.patch) else self.patch
    def exec(self, sid, cmd, user="agent", timeout=60, env=None, cwd=None, landlock=None):
        self.calls.append(("exec", user, cmd[:30]))
        if "rev-parse" in cmd: return {"exit": 0, "stdout": "deadbeef\n", "stderr": "", "timeout": False, "elapsed_s": 0}
        return {"exit": 0, "stdout": "ok", "stderr": "", "timeout": False, "elapsed_s": 1}
    def events(self, sid, since=0):
        rep = self.report(self.last_ticket) if callable(self.report) else self.report                 # a function of the ticket lets two agents of one user report differently
        return [{"seq": 1, "type": "model_call", "data": {}}, {"seq": 2, "type": "note", "data": {"text": "outcome: used pytest -q"}}, {"seq": 3, "type": "report", "data": rep}]
    def ticket(self, sid, t): self.calls.append(("ticket", t["id"])); self.last_ticket = t
    vols = []
    def volumes(self): return [dict(v) for v in self.vols]
    def volume_from_archive(self, name, tar, size_mib=64):
        v = {"id": f"vol-{len(self.vols) + 1}", "name": name, "attached": {}, "size_mib": size_mib}; self.vols.append(v); self.calls.append(("volume_from_archive", name)); return v
    def volume_delete(self, vid):
        self.vols = [v for v in self.vols if v["id"] != vid]; self.calls.append(("volume_delete", vid)); return {"deleted": vid}
    def inbox(self, sid, item): self.inbox_items.append(item)
    def budget(self, sid, usd): pass
    def pack(self, sid, name): return {"id": "L-ws"}
    def destroy(self, sid): self.calls.append(("destroy", sid))


class FakeGitea:
    enabled = True; readable = True; url = "http://g"; repo = "o/r"; bot = "cairn-bot"
    def __init__(self): self.comments = []; self.merged = []                                  # per instance: tests in one process must not see each other's merges
    def whoami(self): return "cairn-bot"
    def clone_tar(self, depth=1): return b"TAR", "deadbeef"
    def comment(self, n, text): self.comments.append((n, text))
    def push_patch(self, branch, patch, base): return "cafebabe"
    def open_pr(self, head, title, body): return {"number": 7, "html_url": "http://g/o/r/pulls/7"}
    def merge_pr(self, n, head=None): self.merged.append(n); return {"ok": True}
    def pr_diff(self, n): return b""
    def pr_files(self, n): return [{"filename": "pkg/mod.py", "status": "modified"}, {"filename": "tests/test_mod.py", "status": "added"}, {"filename": "docs/x.png", "status": "added"}]
    def raw(self, path, ref): return {"pkg/mod.py": b"def f():\n    return 2\n", "tests/test_mod.py": b"from pkg.mod import f\ndef test_f(): assert f() == 2\n"}[path]
    def archive(self, sha):
        import io, tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in (("repo-abc/pkg/mod.py", b"def f():\n    return 1\n"), ("repo-abc/pkg/__init__.py", b""), ("repo-abc/README.md", b"# x\n")):
                ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()


class FakeEval:
    def __init__(self): self.imported = []
    def import_task(self, name, payload): self.imported.append((name, payload)); return {"task": payload["id"], "taskset": name, "n_tasks": len(self.imported), "check_batch": "b-check"}
    def pull(self, n): return {"state": "open", "merged": False}


def engine(report=None):
    d = tempfile.mkdtemp(); cfg = I.cfg_from_env(); cfg["state"] = d; cfg["layers"] = ["L-a"]; cfg["merge_s"] = 0
    return I.Engine(cfg, cp=FakeCP(report), proxy=None, gitea=FakeGitea())


def wait(e, pred, t=5.0):
    t0 = time.time()
    while time.time() - t0 < t:
        if pred(): return True
        time.sleep(0.05)
    return pred()


def gitea_trigger(i, **kw):
    t = {"id": f"gitea-c-{i}", "source": "gitea", "actor": "someone", "human": True, "addressed": False, "scope": "issue_comment", "text": "bug here",
         "url": None, "threads": ["gitea:o/r#1"], "channel": {"kind": "gitea", "issue": 1}, "meta": {}}
    t.update(kw); return t


def test_workbench_request_runs_one_assignment_and_ends_ready():
    e = engine(); t = I.workbench_trigger({"text": "please fix"})
    assert e.receive(t) == "queued"; e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    tk = e.store.all("tickets")[0]
    assert tk["outcome"] == "ready" and tk["workspace_layer"] == "L-ws" and tk["last_report"]["state"] == "ready"
    ops = [c[0] for c in e.cp.calls]
    assert ops.index("ticket") < ops.index("destroy") and ops.count("create") == 1
    assert e.ticket_view(tk["id"], False)["stage"] == "ended"


def test_gate_ignores_unlisted_and_bots_and_accepts_listed_addressed():
    e = engine()
    e.receive(gitea_trigger(1)); e.pass_once()
    assert e.store.all("decisions")[-1]["outcome"] == "ignore"
    e.receive(gitea_trigger(2, actor="fc-runner", human=False, addressed=True)); e.pass_once()
    assert e.store.all("decisions")[-1]["basis"] == "bot not listed"
    assert e.receive(gitea_trigger(2, actor="fc-runner", human=False, addressed=True)) == "duplicate"
    r = e.policy.update({"sources": {"gitea": {"allowed": ["someone"]}}}); assert r["changed"]
    e.receive(gitea_trigger(3, addressed=True, text="@cairn-bot look")); e.pass_once()
    d = e.store.all("decisions")[-1]; assert d["outcome"] == "accept" and d["basis"] == "listed-addressed" and d["by_whom"] == "gate"
    assert wait(e, lambda: e.store.all("tickets")[0]["state"] == "ended")
    e.flush_outbox()
    assert any("Taking this on" in c[1] for c in e.gitea.comments)


def test_followup_resumes_ended_ticket_with_recovery_assignment():
    e = engine(); e.policy.update({"sources": {"gitea": {"allowed": ["someone"]}}})
    e.receive(gitea_trigger(1, addressed=True)); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    assert e.receive(gitea_trigger(2, text="also this")) == "fanned-out"
    tid = e.store.all("tickets")[0]["id"]
    assert wait(e, lambda: len(e.store.all("assignments")) == 2 and e.store.all("assignments")[-1]["state"] == "ended")
    a = e.store.all("assignments")[-1]; assert a["recovery"] and "L-ws" in a["layers"]
    assert len(e.store.get("tickets", tid)["inputs"]) == 2


def test_person_stop_is_final():
    e = engine(); e.policy.update({"sources": {"gitea": {"allowed": ["someone"]}}})
    e.receive(gitea_trigger(1, addressed=True)); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    tid = e.store.all("tickets")[0]["id"]; e.stop_ticket(tid, by="person")
    assert e.receive(gitea_trigger(2, text="continue")) in ("queued", "fanned-out")
    e.pass_once(); time.sleep(0.3)
    assert e.store.get("tickets", tid)["state"] == "ended" and e.store.get("tickets", tid)["ended_by"] == "person"


def test_ready_with_deliverable_opens_pr_and_follows_it():
    e = engine(report={"state": "ready", "text": "fixed", "deliverable": {"branch": "ticket/x", "head": "beef", "base": "dead"}})
    e.receive(I.workbench_trigger({"text": "fix it"})); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and (e.store.all("tickets")[0].get("pr") or {}).get("number") == 7)
    tk = e.store.all("tickets")[0]; assert tk["state"] == "open" and e.ticket_view(tk["id"], False)["stage"] == "person/approve"   # the PR is a merge draft (docs/005 批准)
    e.gitea.pull = lambda n: {"state": "closed", "merged": True}; e.follow_prs()                                                        # merged on the code host by hand: the job ends the same way
    tk = e.store.all("tickets")[0]; assert tk["state"] == "ended" and tk["outcome"] == "merged"


def test_waiting_with_attention_needs_a_person_and_answer_redispatches():
    e = engine(report={"state": "waiting", "text": "QUESTION: which db?", "attention": {"question": "which db?"}})
    e.receive(I.workbench_trigger({"text": "migrate"})); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0].get("attention"))
    tk = e.store.all("tickets")[0]; assert e.ticket_view(tk["id"], False)["stage"] == "person/answer"
    assert tk["id"] in [t["id"] for t in e.attention()["needs_person"]]
    r = e.answer_ticket(tk["id"], "postgres"); assert "assignment" in r


def test_policy_version_is_content_hash():
    e = engine(); v0 = e.policy.version
    assert e.policy.update({"mode": "observe"})["changed"] is False
    r = e.policy.update({"rules": [{"name": "r1", "text": "fix simple bugs"}]}); assert r["changed"] and r["version"] != v0
    assert len(e.store.all("policy_versions")) == 2


class FakeGiteaCI(FakeGitea):
    """Gitea whose main is at one commit; the commit status is whatever the test sets."""
    base = "main"; state = "failure"
    def api(self, method, path, body=None):
        if path.endswith("/branches/main"): return {"commit": {"id": "abc123def4567890"}}
        if "/status" in path: return {"state": self.state, "statuses": [{"status": "failure", "context": "ci/test", "description": "Failing after 14m", "target_url": "/William2333ZZ/cairn/actions/runs/23"}]}
        raise AssertionError(path)


def test_ci_source_files_one_ticket_per_failed_commit_and_schedule_fires_once():
    e = engine(); e.gitea = FakeGiteaCI(); ci = I.CISource(e)
    assert ci.poll_once() == 1 and ci.poll_once() == 0                          # seen commits are not filed again
    e.pass_once(); tk = e.store.all("tickets"); assert len(tk) == 1 and tk[0]["source"] == "ci" and "abc123def4" in tk[0]["title"]
    e.gitea.state = "success"; e.store.kv_set("ci.seen", []); assert ci.poll_once() == 0   # green main files nothing
    e.policy.update({"schedules": [{"id": "nightly", "text": "run the audit", "every_s": 3600, "enabled": True}]})
    sc = I.ScheduleSource(e); assert sc.poll_once() == 1 and sc.poll_once() == 0
    e.pass_once(); assert len(e.store.all("tickets")) == 2 and e.store.all("tickets")[1]["source"] == "schedule"


def test_conversation_rows_and_say_rules():
    """docs/005 对话与路由 + docs/006 说话的规矩: a request is one message; accept adds a work marker and an ack; the end adds one ready say;
    a progress report never speaks; an input to a job is a message tagged with the ticket; the Router merge window holds a person's messages."""
    e = engine(report={"state": "ready", "text": "done", "deliverable": None})
    r = e.converse_in({"text": "please fix the build"}); assert r["disposition"] == "queued"
    e.pass_once(); assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    rows = e.conversation_feed()["items"]
    assert [x["kind"] for x in rows] == ["message", "work", "say", "say"] and [x["say"] for x in rows if x["kind"] == "say"] == ["ack", "ready"]
    assert rows[0]["origin"]["source"] == "workbench" and rows[1]["stage"] == "ended" and rows[1]["short"]
    assert all(x["delivered"] == 1 for x in rows if x["kind"] == "say")              # workbench channel: nothing to send back, nothing queued
    assert not e.store.all("outbox")
    tid = rows[1]["ticket"]; e.converse_in({"text": "also bump the version", "ticket": tid})
    rows = e.conversation_feed()["items"]; assert any(x["kind"] == "message" and x["ticket"] == tid and "bump" in x["text"] for x in rows)
    # a progress report is not a say
    e2 = engine(report={"state": "progress", "text": "half way"}); e2.converse_in({"text": "x"}); e2.pass_once()
    assert wait(e2, lambda: any(a["state"] == "ended" for a in e2.store.all("assignments")))
    assert [x["say"] for x in e2.conversation_feed()["items"] if x["kind"] == "say"] == ["ack"]
    # gitea-origin tickets: the say goes back as a comment too
    e3 = engine(); e3.policy.update({"sources": {**e3.policy.p["sources"], "gitea": {**e3.policy.p["sources"]["gitea"], "allowed": ["someone"]}}})
    e3.receive(gitea_trigger(1, addressed=True)); e3.pass_once()
    assert wait(e3, lambda: e3.store.all("tickets") and e3.store.all("tickets")[0]["state"] == "ended")
    says = [x for x in e3.conversation_feed()["items"] if x["kind"] == "say"]
    assert says and all(x["delivered"] == 0 and x["dest"]["channels"] for x in says) and len(e3.store.all("outbox")) == len(says)
    # merge window: a fresh person's message is not evaluated before merge_s
    e4 = engine(); e4.cfg["merge_s"] = 100; e4.converse_in({"text": "y"}); e4.evaluate_due(); assert not e4.store.all("tickets")
    e4.cfg["merge_s"] = 0; e4.evaluate_due(); assert e4.store.all("tickets")


def test_pr_drafts_a_merge_approval_and_consent_merges_it():
    """docs/005 批准: opening a PR is the draft of a merge; nothing merges until a person approves; a denial leaves the job in review; a stop expires the draft."""
    e = engine(report={"state": "ready", "text": "fixed", "deliverable": {"branch": "ticket/x", "head": "beef", "base": "dead"}})
    e.receive(I.workbench_trigger({"text": "fix it"})); e.pass_once()
    assert wait(e, lambda: e.store.all("approvals"))
    ap = e.store.all("approvals")[0]; tk = e.store.all("tickets")[0]
    assert ap["kind"] == "merge" and ap["state"] == "pending" and ap["action"]["pr"] == 7 and e.ticket_view(tk["id"], False)["stage"] == "person/approve"
    says = [r for r in e.conversation_feed()["items"] if r["kind"] == "say" and r["say"] == "approval"]; assert says and says[0]["meta"]["approval"] == ap["id"]
    assert tk["id"] in [t["id"] for t in e.attention()["needs_person"]]
    r = e.decide_approval(ap["id"], "approve", actor="me")
    assert r["state"] == "approved" and e.gitea.merged == [7] and not r.get("error")
    tk = e.store.get("tickets", tk["id"]); assert tk["state"] == "ended" and tk["outcome"] == "merged" and tk["pr"]["state"] == "merged"
    # a second consent on the same approval is refused
    try: e.decide_approval(ap["id"], "approve"); raise AssertionError("decided twice")
    except ValueError: pass
    # deny: the job stays open for review; stop: the draft expires
    e2 = engine(report={"state": "ready", "text": "fixed", "deliverable": {"branch": "ticket/x", "head": "beef", "base": "dead"}}); e2.gitea.merged = []
    e2.receive(I.workbench_trigger({"text": "fix it"})); e2.pass_once(); assert wait(e2, lambda: e2.store.all("approvals"))
    ap2 = e2.store.all("approvals")[0]; e2.decide_approval(ap2["id"], "deny")
    tk2 = e2.store.all("tickets")[0]; assert e2.ticket_view(tk2["id"], False)["stage"] == "person/review" and not e2.gitea.merged
    e3 = engine(report={"state": "ready", "text": "fixed", "deliverable": {"branch": "ticket/x", "head": "beef", "base": "dead"}})
    e3.receive(I.workbench_trigger({"text": "fix it"})); e3.pass_once(); assert wait(e3, lambda: e3.store.all("approvals"))
    e3.stop_ticket(e3.store.all("tickets")[0]["id"]); e3.pass_once(); assert e3.store.all("approvals")[0]["state"] == "expired"


def test_harness_approval_attention_waits_for_consent_then_continues():
    """A harness that must send outside the repo drafts it (attention kind approval); approval sends through the relay and the answer goes back to the job as an Input."""
    e = engine(report={"state": "waiting", "text": "say hi", "attention": {"kind": "approval", "action": {"kind": "send", "to": {"kind": "gitea", "issue": 1}, "text": "hi there"}, "draft": "comment on issue #1: hi there"}})
    e.receive(I.workbench_trigger({"text": "greet the reporter"})); e.pass_once()
    assert wait(e, lambda: e.store.all("approvals"))
    ap = e.store.all("approvals")[0]; tk = e.store.all("tickets")[0]
    assert ap["kind"] == "send" and e.ticket_view(tk["id"], False)["stage"] == "person/approve" and not tk.get("attention")
    r = e.decide_approval(ap["id"], "approve", actor="me")
    assert r["state"] == "approved" and e.gitea.comments[-1] == (1, "hi there")
    tk = e.store.get("tickets", tk["id"]); assert any(i.get("kind") == "answer" and "approved" in i["text"] for i in tk["inputs"])


def test_memory_two_moments_and_profile_edits():
    """docs/005 记忆: when a job ends its notes and one log line are kept; the next job starts with them in ticket.json's memory section; the person edits the profile."""
    e = engine(); e.converse_in({"text": "run the tests"}); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    m = e.memory_view()
    assert [n["text"] for n in m["notes"]] == ["outcome: used pytest -q"] and len(m["log"]) == 1 and "run the tests" in m["log"][0]["text"] and "ready" in m["log"][0]["text"]
    assert e.cp.last_ticket["memory"] == {"profile": [], "log": [], "notes": [], "rules": []}            # the first job started with nothing
    e.memory_put_profile([{"text": "Prefers pytest -q and small commits"}])
    e.converse_in({"text": "add a test"}); e.pass_once()
    assert wait(e, lambda: len(e.store.all("tickets")) == 2 and e.store.all("tickets")[1]["state"] == "ended")
    mem = e.cp.last_ticket["memory"]
    assert mem["profile"] == ["Prefers pytest -q and small commits"] and len(mem["log"]) == 1 and mem["notes"] == []   # the ended job's notes are not "other open jobs"
    fid = e.memory_view()["profile"][0]["id"]; e.memory_delete(fid); assert e.memory_view()["profile"] == []


def test_recurring_request_becomes_a_routine_draft_that_consent_turns_on():
    """docs/005 例行: "以后每天早上八点……" is a routine draft, not a job; approval writes it into the policy; the schedule source then files it."""
    assert I.parse_routine("以后每天早上八点,把昨晚的 CI 失败整理给我") == {"text": "以后每天早上八点,把昨晚的 CI 失败整理给我", "enabled": True, "daily_at": "08:00"}
    assert I.parse_routine("每 30 分钟看一眼告警")["every_s"] == 1800 and I.parse_routine("晚上 9:30 每天跑一遍测试")["daily_at"] == "21:30"
    assert I.parse_routine("修一下登录页") is None and I.parse_routine("每周一汇总") is None
    e = engine(); r = e.converse_in({"text": "每 1 分钟看一眼仓库里有没有新 issue"})
    assert r["disposition"] == "routine-draft" and not e.store.all("tickets")
    ap = e.store.all("approvals")[0]; assert ap["kind"] == "routine" and ap["action"]["schedule"]["every_s"] == 60 and ap["ticket"] is None
    assert [x["say"] for x in e.conversation_feed()["items"] if x["kind"] == "say"] == ["approval"]
    e.pass_once(); assert e.store.all("approvals")[0]["state"] == "pending"                 # no job to end, so the draft does not expire
    e.decide_approval(ap["id"], "approve", actor="me")
    rt = e.routines(); assert len(rt) == 1 and rt[0]["every_s"] == 60 and rt[0]["enabled"]
    sc = I.ScheduleSource(e); assert sc.poll_once() == 1; e.pass_once()
    assert e.store.all("tickets") and e.store.all("tickets")[0]["source"] == "schedule"
    e.routine_update(rt[0]["id"], {"enabled": False}); assert e.routines()[0]["enabled"] is False
    e.routine_update(rt[0]["id"], {"delete": True}); assert e.routines() == []


def test_update_switches_the_agent_version_unless_pinned_and_rollback_pins_the_previous():
    """docs/007 对用户的更新与回退: a promotion is one update say; agents following stable switch (new jobs run the new layers); "退回" pins the previous."""
    e = engine()
    r = e.update({"version": "v-new1", "label": "v1", "layers": ["L-langs", "L-h1"], "changes": "observation formatting", "stats": {"mean": 0.82, "parent_mean": 0.71, "ci": [0.02, 0.2], "cost": 0.02, "parent_cost": 0.027}, "taskset": "polyglot-py"})
    assert r["applied"] and e.agent_version()["id"] == "v-new1"
    says = [x for x in e.conversation_feed()["items"] if x["kind"] == "say"]; assert says[-1]["say"] == "update" and "0.71 → 0.82" in says[-1]["text"] and says[-1]["meta"]["update"] == "v-new1"
    e.converse_in({"text": "fix the build"}); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended"); assert e.store.all("tickets")[0]["mandate"]["version"]["id"] == "v-new1"
    assert [c for c in e.cp.calls if c[0] == "create"][0][1]["layers"][:2] == ["L-langs", "L-h1"]   # the job ran the agent's version
    e.update({"version": "v-new2", "label": "v2", "layers": ["L-langs", "L-h2"], "changes": "x", "stats": {}})
    assert e.agent_version()["id"] == "v-new2" and e.agent_view()["previous"]["id"] == "v-new1"
    assert e.converse_in({"text": "退回上一版"})["disposition"] == "rollback"
    v = e.agent_view(); assert v["version"]["id"] == "v-new1" and v["pinned"] and v["previous"]["id"] == "v-new2"
    r3 = e.update({"version": "v-new3", "label": "v3", "layers": ["L-langs", "L-h3"], "changes": "y", "stats": {}})
    assert r3["applied"] is False and e.agent_version()["id"] == "v-new1" and len(e.agent_view()["updates"]) == 3
    e.set_agent_version(None, pin=False); assert e.agent_version() is None


def test_rows_and_keys_belong_to_a_user():
    """docs/009 鉴权与隔离: every table carries the user; a store reads only its user's rows; kv keys are per user; old keys migrate to the default user."""
    d = tempfile.mkdtemp(); path = os.path.join(d, "intake.sqlite")
    legacy = I.Store(path); legacy.db.execute("INSERT INTO kv(k,v) VALUES('ci.seen','[\"abc\"]')")      # a key from before users existed
    s1 = I.Store(path, user="u1"); s2 = I.Store(path, user="u2"); s0 = I.Store(path)
    s1.insert("tickets", {"id": "t-1", "short": "1", "title": "x", "thread": "th", "source": "workbench", "state": "open", "created": 1, "updated": 1})
    assert s1.get("tickets", "t-1")["user"] == "u1" and s2.get("tickets", "t-1") is None and s2.all("tickets") == [] and len(s1.all("tickets", "WHERE state='open'")) == 1
    s1.kv_set("agent.pinned", True); assert s1.kv_get("agent.pinned") is True and s2.kv_get("agent.pinned") is None
    assert s0.kv_get("ci.seen") == ["abc"]                                                   # migrated to default:ci.seen
    e = engine(); assert e.user == "default"; e.converse_in({"text": "hi"}); assert e.store.all("conversation")[0]["user"] == "default"


def test_merged_pr_with_tests_is_mined_into_the_repository_taskset():
    """docs/007 任务从哪来: consent merges the PR; the job becomes a task: workspace at the merge base without the tests, the PR's tests hidden, its source as reference."""
    e = engine(report={"state": "ready", "text": "fixed", "deliverable": {"branch": "ticket/x", "head": "beef", "base": "dead"}}); e.evalc = FakeEval()
    e.gitea.pull = lambda n: {"state": "open", "merged": False, "merge_base": "abc", "head": {"sha": "beef"}, "base": {"sha": "abc"}}
    e.converse_in({"text": "make f return 2"}); e.pass_once(); assert wait(e, lambda: e.store.all("approvals"))
    e.decide_approval(e.store.all("approvals")[0]["id"], "approve", actor="me")
    assert len(e.evalc.imported) == 1; name, p = e.evalc.imported[0]
    assert name == "user-o-r" and p["verify_files"] == {"tests/test_mod.py": "from pkg.mod import f\ndef test_f(): assert f() == 2\n"} and p["reference_files"] == {"pkg/mod.py": "def f():\n    return 2\n"}
    assert "make f return 2" in p["prompt"] and p["source"]["pr"] == 7 and p["source"]["base"] == "abc"
    tk = e.store.all("tickets")[0]; assert tk["task"]["taskset"] == "user-o-r" and tk["task"]["check_batch"] == "b-check"
    assert any(r["kind"] == "note" and "评测题" in r["text"] for r in e.conversation_feed()["items"])
    assert I.classify_pr_files([{"filename": "a/b.py", "status": "modified"}, {"filename": "tests/test_a.py", "status": "removed"}]) == ([], ["a/b.py"])
    # a PR without python tests is not a task, and says why on the ticket
    e2 = engine(report={"state": "ready", "text": "fixed", "deliverable": {"branch": "ticket/x", "head": "beef", "base": "dead"}}); e2.evalc = FakeEval()
    e2.gitea.pull = lambda n: {"state": "closed", "merged": True, "merge_base": "abc", "head": {"sha": "beef"}}; e2.gitea.pr_files = lambda n: [{"filename": "README.md", "status": "modified"}]
    e2.converse_in({"text": "docs"}); e2.pass_once(); assert wait(e2, lambda: e2.store.all("tickets") and e2.store.all("tickets")[0].get("pr"))
    e2.follow_prs(); assert e2.evalc.imported == [] and e2.store.all("tickets")[0]["task"] == {"skipped": "no python tests"}


def test_github_connector_has_the_same_shape_and_the_policy_gets_its_section():
    """docs/002 Connector: GitHub is the Gitea method set over another API; the deployment picks one with CODE_HOST; a saved policy gains the new source section."""
    cfg = I.cfg_from_env(); cfg.update({"code_host": "github", "github_repo": "o/r", "github_bot": "bot", "github_token_file": ""})
    g = I.make_connector(cfg); assert isinstance(g, I.GitHub) and g.kind == "github" and g.readable and not g.enabled
    calls = []
    g.api = lambda method, path, body=None, timeout=30: (calls.append((method, path, body)) or ({"base": {"sha": "b1"}, "head": {"sha": "h1"}} if path.endswith("/pulls/4") else {}))
    assert g.pull(4)["merge_base"] == "b1" and g.merge_pr(4) == {} and calls[-1] == ("PUT", "/repos/o/r/pulls/4/merge", {"merge_method": "merge"})
    g.open_pr("ticket/x", "t", "b"); assert calls[-1][1] == "/repos/o/r/pulls" and calls[-1][2]["base"] == cfg["base_branch"]
    assert g.clone_url() == "https://github.com/o/r.git"; g.token = "tok"; assert g.clone_url() == "https://x-access-token:tok@github.com/o/r.git"
    assert I.make_connector({**cfg, "code_host": "gitea"}).kind == "gitea"
    try: I.make_connector({**cfg, "code_host": "svn"}); raise AssertionError("accepted an unknown host")
    except ValueError: pass
    e = engine(); assert e.policy.is_allowed("github", "nobody") is False and "github" in e.policy.p["sources"]
    # the connectors view and pausing the code host's poller
    v = e.connectors_view()["connectors"]; assert [c["kind"] for c in v] == ["workbench", "gitea", "chat"] and v[1]["bot"] == "cairn-bot" and v[1]["paused"] is False
    e.connector_pause("gitea", True); assert e.connectors_view()["connectors"][1]["paused"] is True
    src = I.GiteaSource(e); assert src.paused() and src.poll_once() == 0


def test_one_engine_per_user_on_one_deployment():
    """docs/009: users share the sandboxes, model proxy and code host but not their jobs, conversation, routines or memory; X-User picks the Engine."""
    cfg = I.cfg_from_env(); cfg["state"] = tempfile.mkdtemp(); cfg["layers"] = ["L-a"]; cfg["merge_s"] = 0; cfg["cp_token"] = ""   # no bearer in this test, whatever the environment has
    es = I.Engines(cfg, cp=FakeCP(), proxy=None, gitea=FakeGitea(), evalc=FakeEval())
    a, b = es.get("alice"), es.get("bob"); assert a is not b and es.get("alice") is a and es.default.user == "default"
    a.converse_in({"text": "alice's job"}); a.pass_once(); assert wait(a, lambda: a.store.all("tickets"))
    assert b.store.all("tickets") == [] and b.conversation_feed()["items"] == [] and a.store.all("tickets")[0]["user"] == "alice"
    a.memory_put_profile([{"text": "likes pytest"}]); assert b.memory_view()["profile"] == [] and es.users() == ["alice", "bob", "default"]
    try: es.get("Not Valid!"); raise AssertionError("accepted a bad user name")
    except KeyError: pass
    import io
    class Req(I.Handler):
        def __init__(self, user): self.path = "/tickets"; self.headers = {"Content-Length": "0", "X-User": user}; self.rfile = io.BytesIO(b""); self.out = None
        def _send(self, c, o): self.out = (c, o)
    Req.engines = es; Req.e = es.default
    r = Req("alice"); r._dispatch("GET"); assert r.out[0] == 200 and len(r.out[1]["tickets"]) == 1
    r = Req("bob"); r._dispatch("GET"); assert r.out[0] == 200 and r.out[1]["tickets"] == []
    r = Req("bad user"); r._dispatch("GET"); assert r.out[0] == 400


def test_chat_connector_hears_listed_people_and_answers_where_they_spoke():
    """docs/002 Connector (chat): the tool's webhook brings a message in; a listed person's DM is a request; the agent's words go back out through the tool's webhook."""
    sent = []
    class Chat(I.ChatConnector):
        def __init__(self): super().__init__({"chat_format": "feishu", "chat_webhook_url": "http://chat.invalid/hook", "chat_secret_file": "", "chat_bot": "cairn"}); self.secret = "s3"; self.readable = True
        def send(self, text, thread=None): sent.append((thread, text)); return b""
    e = engine(); e.chat = Chat()
    e.policy.update({"sources": {**e.policy.p["sources"], "chat": {**e.policy.p["sources"]["chat"], "allowed": ["ou_alice"]}}})
    # handshake, bad secret, parsing
    assert e.chat_hook({"X-Chat-Secret": "s3"}, {"type": "url_verification", "challenge": "c1", "token": "s3"}) == {"challenge": "c1"}
    try: e.chat_hook({}, {"text": "hi"}); raise AssertionError("accepted without the secret")
    except PermissionError: pass
    ev = {"schema": "2.0", "header": {"event_type": "im.message.receive_v1", "token": "s3"}, "event": {"sender": {"sender_id": {"open_id": "ou_alice"}}, "message": {"message_id": "om_1", "chat_id": "oc_9", "chat_type": "p2p", "message_type": "text", "content": "{\"text\": \"修一下登录页的报错\"}"}}}
    r = e.chat_hook({}, ev); assert r["disposition"] == "queued" and r["trigger"] == "chat-om_1"
    e.pass_once(); assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    t = e.store.all("tickets")[0]; assert t["source"] == "chat" and t["channels"] == [{"kind": "chat", "thread": "oc_9"}]
    e.flush_outbox(); assert sent and sent[0][0] == "oc_9" and "Taking this on" in sent[0][1]
    # an unlisted person in a group without @mention is ignored; the generic format and Slack parse too
    ev2 = {**ev, "event": {"sender": {"sender_id": {"open_id": "ou_bob"}}, "message": {"message_id": "om_2", "chat_id": "oc_9", "chat_type": "group", "content": "{\"text\": \"hello\"}"}}}
    assert e.chat_hook({}, ev2)["disposition"] in ("queued", "fanned-out"); e.pass_once(); assert len(e.store.all("tickets")) == 1
    g = I.ChatConnector({"chat_format": "generic", "chat_webhook_url": "", "chat_secret_file": "", "chat_bot": "cairn"})
    assert g.parse({"actor": "u1", "text": "x", "thread": "th"}) == {"actor": "u1", "text": "x", "thread": "th", "dm": True, "id": None} and g.payload("hi") == {"text": "hi", "from": "cairn"}
    sl = I.ChatConnector({"chat_format": "slack", "chat_webhook_url": "", "chat_secret_file": "", "chat_bot": "cairn"})
    assert sl.parse({"event": {"type": "message", "user": "U1", "text": "@cairn fix it", "channel": "C1", "channel_type": "channel", "ts": "1.2"}})["dm"] is False and sl.payload("x") == {"text": "x"}
    assert sl.trigger(sl.parse({"event": {"type": "message", "user": "U1", "text": "@cairn fix it", "channel": "C1", "channel_type": "channel", "ts": "1.2"}}))["addressed"] is True


def test_a_user_can_have_agents_with_roles_and_address_them_by_name():
    """docs/001 多个 agent: besides main, named agents with a role; each has its own jobs, memory and routines; "@fix …" goes to fix; the role reaches the harness."""
    cfg = I.cfg_from_env(); cfg["state"] = tempfile.mkdtemp(); cfg["layers"] = ["L-a"]; cfg["merge_s"] = 0; cfg["cp_token"] = ""
    es = I.Engines(cfg, cp=FakeCP(), proxy=None, gitea=FakeGitea(), evalc=FakeEval())
    assert [a["name"] for a in es.agents("default")] == ["main"]
    try: es.get("default", "fix"); raise AssertionError("an unknown agent was created")
    except KeyError: pass
    es.agent_put("default", "fix", "你只修 bug,不做新功能;改动尽量小。")
    fix = es.get("default", "fix"); assert fix is es.get("default", "fix") and fix.user == "default/fix" and fix.cfg["owner"] == "default"
    assert es.route("default", "@fix 修一下登录页") == ("fix", "修一下登录页") and es.route("default", "@nobody x") == ("main", "@nobody x")
    import io
    class Req(I.Handler):
        def __init__(self, text): self.path = "/conversation"; b = json.dumps({"text": text}).encode(); self.headers = {"Content-Length": str(len(b)), "X-User": "default"}; self.rfile = io.BytesIO(b); self.out = None
        def _send(self, c, o): self.out = (c, o)
    Req.engines = es; Req.e = es.default
    r = Req("@fix 修一下登录页"); r._dispatch("POST"); assert r.out[0] == 201
    fix.pass_once(); assert wait(fix, lambda: fix.store.all("tickets") and fix.store.all("tickets")[0]["state"] == "ended")
    assert fix.store.all("tickets")[0]["title"].startswith("修一下登录页") and es.default.store.all("tickets") == []     # the job is fix's, not main's
    assert es.cp.last_ticket["agent"] == "fix" and "只修 bug" in es.cp.last_ticket["role"]
    assert fix.memory_view()["log"] and es.default.memory_view()["log"] == []
    es.agent_delete("default", "fix"); assert [a["name"] for a in es.agents("default")] == ["main"]
    try: es.agent_delete("default", "main"); raise AssertionError("deleted main")
    except ValueError: pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn): fn()
    print("intake unit ok")


def test_ticket_view_replies_come_from_the_conversation_and_old_rows_still_decode():
    """test-intake reads a ticket's replies as purposes (acknowledgement / result): they are the ticket's say rows, whatever channel they went to.
    A policy_versions row written before `origin` became a JSON column is a plain string and must still read back."""
    e = engine(report={"state": "ready", "text": "done", "deliverable": None})
    e.converse_in({"text": "list the files"}); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[0]["state"] == "ended")
    v = e.ticket_view(e.store.all("tickets")[0]["id"])
    assert {m["purpose"] for m in v["replies"]} >= {"acknowledgement", "result"} and all(m["state"] == "sent" for m in v["replies"])
    with e.store.lock: e.store.db.execute("INSERT INTO policy_versions(id, policy, origin, created, user) VALUES('P-old', '{}', 'panel', 1, 'default')")
    old = [r for r in e.store.all("policy_versions", order="created DESC") if r["id"] == "P-old"]
    assert old and old[0]["origin"] == "panel" and old[0]["policy"] == {}
