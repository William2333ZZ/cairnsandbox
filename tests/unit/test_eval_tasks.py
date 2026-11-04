"""Kinds of task (docs/007 任务的种类, D17): dependent tasks carry the user's rules, obstacle tasks are a conversation scored by Ask-F1,
long tasks report their parts. No services: the Runner's two phases are replaced by fakes."""
import json, os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import evaluator as ev  # noqa: E402


def mk_tar(files):
    import io, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


WS1, WS2 = mk_tar({"a.txt": b"guess"}), mk_tar({"a.txt": b"answered"})


def make_task(**meta):
    d = tempfile.mkdtemp(); tdir = os.path.join(d, "t1"); os.makedirs(os.path.join(tdir, "init")); os.makedirs(os.path.join(tdir, "verify"))
    json.dump({"id": "t1", "domain": "coding", "group": "evolve", "timeout_s": 60, **meta}, open(os.path.join(tdir, "meta.json"), "w")); open(os.path.join(tdir, "prompt.md"), "w").write("Add a file.")
    return ev.Task(tdir)


def test_bigram_overlap_matches_a_question_to_the_blocker_it_is_about():
    b = [{"question": "Which config file should hold the new setting, pyproject.toml or setup.cfg?", "answer": "pyproject.toml"},
         {"question": "要不要同时更新 CHANGELOG?", "answer": "要"}]
    assert ev.match_blocker("Should the setting go in pyproject.toml or setup.cfg?", b, set()) == 0
    assert ev.match_blocker("CHANGELOG 要不要一起改?", b, set()) == 1
    assert ev.match_blocker("What colour should the button be?", b, set()) is None
    assert ev.match_blocker("Should the setting go in pyproject.toml or setup.cfg?", b, {0}) is None            # a blocker is answered once
    assert ev.question_of({"info": {"submission": "QUESTION: which file?"}}) == "which file?" and ev.question_of({"info": {"submission": "done"}}) is None and ev.question_of(None) is None


def test_ask_f1_rewards_the_right_questions_and_punishes_silence_and_noise():
    blockers = [{"question": "a", "answer": "b"}, {"question": "c", "answer": "d"}]
    assert ev.ask_metrics([], blockers) == {"asked": 0, "matched": 0, "blockers": 2, "hit": 0, "precision": None, "recall": 0.0, "f1": 0.0}     # guessed without asking
    m = ev.ask_metrics([{"q": "x", "blocker": 0}, {"q": "y", "blocker": None}], blockers)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and abs(m["f1"] - 0.5) < 1e-9                                                           # one good question, one noise
    m = ev.ask_metrics([{"q": "x", "blocker": 0}, {"q": "z", "blocker": 1}], blockers); assert m["f1"] == 1.0
    assert ev.ask_metrics([{"q": "n", "blocker": None}], blockers)["f1"] == 0.0


def test_an_obstacle_task_is_a_conversation_and_the_verified_workspace_is_the_last_one():
    t = make_task(kind="obstacle", blockers=[{"question": "Which file holds the setting, pyproject.toml or setup.cfg?", "answer": "pyproject.toml"}])
    assert t.kind == "obstacle" and t.blockers and t.info()["n_blockers"] == 1
    prompts = []
    class FakeRunner(ev.Runner):
        def agent_phase(self, task, version, memory=True, extra="", ask="v1"):
            prompts.append(task.prompt + (("\n\n" + ev.ASK_RULE) if task.blockers else "") + extra)
            asks_now = "The user answered" not in extra
            sub = "QUESTION: Should the setting go in pyproject.toml or setup.cfg?" if asks_now else "Submitted: wrote pyproject.toml"
            return {"ws": WS1 if asks_now else WS2, "traj": {"info": {"submission": sub}}, "agent_exit": 0, "agent_timeout": False, "events": [], "agent_log": "", "elapsed_s": 1.0,
                    "sandbox_id": "s", "node": "n", "cost": 0.01, "tokens_in": 100, "tokens_out": 10}
        def verify_phase(self, task, version, ws_tar):
            return {"exit": 0, "stdout_tail": "", "stderr_tail": "", "timeout": False, "score": 1.0 if ws_tar == WS2 else 0.0, "subtasks": {"tests/test_a.py": 1.0}, "elapsed_s": 0.1}
    r = FakeRunner(None, {}).run_once(t, {})
    assert len(prompts) == 2 and "The user answered: pyproject.toml" in prompts[1] and ev.ASK_RULE in prompts[0]
    assert r["pass"] and r["cost"] == 0.02 and r["verify"]["ask"]["f1"] == 1.0 and r["verify"]["ask"]["asked"] == 1 and r["verify"]["subtasks"] == {"tests/test_a.py": 1.0}
    # an agent that never asks guesses: the hidden test fails and the run says it never asked
    class Guesser(FakeRunner):
        def agent_phase(self, task, version, memory=True, extra="", ask="v1"):
            a = FakeRunner.agent_phase(self, task, version, memory, extra); a["traj"] = {"info": {"submission": "done"}}; a["ws"] = WS1; return a
    g = Guesser(None, {}).run_once(t, {}); assert not g["pass"] and g["verify"]["ask"]["f1"] == 0.0 and g["verify"]["ask"]["asked"] == 0
    # an ordinary task never enters the conversation, even if the agent submits a question
    plain = make_task()
    assert "ask" not in FakeRunner(None, {}).run_once(plain, {})["verify"]


def test_dependent_tasks_run_with_the_rules_and_the_control_arm_without():
    t = make_task(kind="dependent", rules=["Use tabs.", "Keep f's signature."])
    seen = []
    class B:
        users = True; last_node = "n"
        def create(self, spec): return "sb"
        def path(self, sid, p): return p
        def put(self, sid, path, data):
            if path.endswith("prompt.md"): seen.append(data.decode())
        def exec(self, *a, **k): return {"exit": 0, "stdout": "", "stderr": "", "timeout": False}
        def get(self, sid, path): return b"{}" if path.endswith("traj.json") else b"TAR"
        def events(self, sid): return []
        def destroy(self, sid): pass
    r = ev.Runner(B(), {"model_proxy": "", "model_name": "m", "template": "t", "landlock": True})
    r.agent_phase(t, {"layers": [], "agent_cmd": "true"}, memory=True); r.agent_phase(t, {"layers": [], "agent_cmd": "true"}, memory=False)
    assert "Keep f's signature." in seen[0] and "corrected you before" in seen[0] and seen[1] == "Add a file."


def test_subtask_lines_and_the_mined_verify_script_give_partial_credit_per_test_file():
    assert ev.parse_subtasks("x\nSUBTASK tests/test_a.py 1.0\nSUBTASK tests/test_b.py 0.5\nSCORE 0.75\n") == {"tests/test_a.py": 1.0, "tests/test_b.py": 0.5}
    if not shutil.which("uv"): return
    d = tempfile.mkdtemp(); ws, vd = os.path.join(d, "ws"), os.path.join(d, "verify"); os.makedirs(os.path.join(ws, "pkg")); os.makedirs(os.path.join(vd, "tests"))
    open(os.path.join(ws, "pkg", "__init__.py"), "w").write(""); open(os.path.join(ws, "pkg", "mod.py"), "w").write("def f(): return 1\n")
    open(os.path.join(vd, "tests", "test_a.py"), "w").write("from pkg.mod import f\ndef test_1(): assert f() == 1\ndef test_2(): assert f() == 1\n")
    open(os.path.join(vd, "tests", "test_b.py"), "w").write("from pkg.mod import f\ndef test_3(): assert f() == 2\n")
    open(os.path.join(vd, "tests.txt"), "w").write("tests/test_a.py tests/test_b.py\n"); open(os.path.join(vd, "verify.sh"), "w").write(ev.MINED_VERIFY_SH)
    p = subprocess.run(["uv", "run", "--python", "3.12", "--with", "pytest", "bash", os.path.join(vd, "verify.sh"), ws], capture_output=True, text=True, timeout=120)
    lines = p.stdout.strip().splitlines()
    assert lines[-1] == "SCORE 0.6667", p.stdout + p.stderr
    assert ev.parse_subtasks(p.stdout) == {"tests/test_a.py": 1.0, "tests/test_b.py": 0.0}


def test_import_task_records_kind_rules_and_blockers_and_summaries_split_by_kind():
    import base64, io, tarfile
    st = tempfile.mkdtemp(); cfg = ev.cfg_from_env(); cfg["state"] = st; cfg["model_proxy"] = ""
    e = ev.Evaluator(cfg, backend=ev.LocalBackend()); e.submit_batch = lambda body: "b-check"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"def f():\n    return 1\n"; ti = tarfile.TarInfo("repo-x/pkg/mod.py"); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    body = {"id": "t-1", "prompt": "do it", "init_tar_gz_b64": base64.b64encode(buf.getvalue()).decode(), "verify_files": {"tests/test_mod.py": "def test_f(): pass\n"}, "reference_files": {},
            "kind": "dependent", "rules": ["Use tabs."], "blockers": [{"question": "q?", "answer": "a"}, {"question": "", "answer": "x"}]}
    r = e.import_task("user-o-r", body); assert r["task"] == "t-1"
    t = e.taskset("user-o-r")["tasks"][0]
    assert t.kind == "dependent" and t.rules == ["Use tabs."] and t.blockers == [{"question": "q?", "answer": "a"}] and t.info()["kind"] == "dependent"
    # per-kind means and Ask-F1 land in the batch summary
    bid = "b-1"; e.store.insert("batches", {"id": bid, "kind": "adhoc", "taskset": "user-o-r", "version": None, "k": 1, "label": "", "status": "done", "created": 1.0, "params": {}, "progress": {}, "stats": None, "summary": None})
    e.store.insert("runs", {"id": "r-1", "batch": bid, "task": "t-1", "version": "v", "k_index": 0, "stage": "full", "status": "done", "score": 0.5, "pass": 0, "infra_fail": 0, "attempts": 1, "cost": 0.1, "tokens_in": 1, "tokens_out": 1,
                            "verify": {"ask": {"asked": 1, "precision": 1.0, "recall": 0.5, "f1": 2 / 3}}, "created": 1.0})
    s = e._summary(bid, {"mean": 0.5})
    assert s["by_kind"] == {"dependent": {"mean": 0.5, "n": 1}} and abs(s["ask"]["f1"] - 2 / 3) < 1e-9 and s["ask"]["runs"] == 1


def test_the_words_that_teach_the_harness_to_ask_are_the_same_in_a_job_and_in_an_eval_run_and_the_command_really_submits():
    from aios import run as R, env as E
    assert R.ASK_RULE == ev.ASK_RULE and R.ASK_HOW == ev.ASK_HOW and R.ASK_RULE_V2 == ev.ASK_RULE_V2 and ev.ASK_RULES == {"v1": ev.ASK_RULE, "v2": ev.ASK_RULE_V2}
    txt = R.ticket_text({"text": "x", "agents": [{"name": "fix", "role": ""}]})
    assert R.ASK_HOW in txt and "How to finish" in txt and "HANDOFF: <agent>" in txt and "APPROVE: send|budget|routine" in txt
    # the command the prompt shows is what the environment recognises: first output line = marker, the rest = the message
    le = E.LocalEnvironment(cwd=tempfile.mkdtemp())
    for msg in ("QUESTION: which file, pyproject.toml or setup.cfg?", "HANDOFF: fix\nthe login page crashes", "fixed the crash in login.py"):
        try: le.execute({"command": "cat <<'EOF'\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n" + msg + "\nEOF"}); raise AssertionError("did not submit")
        except E.Submitted as s: assert s.submission.strip() == msg


def test_a_real_git_archive_with_a_prefix_is_unpacked_to_its_root_and_paths_cannot_escape():
    """`git archive --prefix=repo/` starts with the directory entry `repo`: the workspace is what is inside it, not a `repo/` nested in it
    (that nesting made every mined task unsolvable); names a caller supplies stay inside the task directory."""
    import base64, subprocess
    d = tempfile.mkdtemp()
    def sh(*a): subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    sh("init", "-q", "-b", "main"); os.makedirs(os.path.join(d, "pkg")); open(os.path.join(d, "pkg", "mod.py"), "w").write("def f(): return 1\n"); open(os.path.join(d, "README.md"), "w").write("x\n"); sh("add", "-A"); sh("commit", "-qm", "c")
    tar = subprocess.run(["git", "-C", d, "archive", "--format=tar.gz", "--prefix=repo/", "HEAD"], capture_output=True, check=True).stdout
    st = tempfile.mkdtemp(); cfg = ev.cfg_from_env(); cfg["state"] = st; cfg["model_proxy"] = ""
    e = ev.Evaluator(cfg, backend=ev.LocalBackend()); e.submit_batch = lambda body: "b"
    body = {"id": "t-1", "prompt": "p", "init_tar_gz_b64": base64.b64encode(tar).decode(), "verify_files": {"tests/test_mod.py": "def test_f(): pass\n", "../../evil.py": "x"}, "reference_files": {"pkg/mod.py": "def f(): return 2\n", "../../../../escaped.txt": "x"}}
    e.import_task("user-x", body); base = os.path.join(st, "tasksets", "user-x", "t-1")
    assert os.path.exists(os.path.join(base, "init", "pkg", "mod.py")) and not os.path.exists(os.path.join(base, "init", "repo"))
    assert not os.path.exists(os.path.join(st, "escaped.txt")) and not os.path.exists(os.path.join(st, "..", "escaped.txt")) and os.listdir(os.path.join(base, "verify")).count("evil.py") == 0
    for bad in ("..", "a/b", "", ".hidden"):
        try: e.import_task(bad, body); raise AssertionError("accepted taskset name " + bad)
        except ValueError: pass
    try: e.import_task("user-x", {**body, "id": ".."}); raise AssertionError("accepted id ..")
    except ValueError: pass


def test_the_grading_run_is_not_configured_by_the_tree_it_grades_and_skips_do_not_pass():
    if not shutil.which("uv"): return
    d = tempfile.mkdtemp(); ws, vd = os.path.join(d, "ws"), os.path.join(d, "verify"); os.makedirs(os.path.join(ws, "pkg")); os.makedirs(os.path.join(vd, "tests"))
    open(os.path.join(ws, "pkg", "__init__.py"), "w").write(""); open(os.path.join(ws, "pkg", "mod.py"), "w").write("def f(): return 1\n")
    open(os.path.join(ws, "conftest.py"), "w").write("import pytest\ndef pytest_collection_modifyitems(items):\n    for i in items: i.add_marker(pytest.mark.skip)\n")          # an agent that tries to skip its way to a score
    open(os.path.join(vd, "tests", "test_a.py"), "w").write("import pytest\nfrom pkg.mod import f\ndef test_1(): assert f() == 2\ndef test_2(): assert f() == 1\n@pytest.mark.skip\ndef test_3(): pass\n")
    open(os.path.join(vd, "tests.txt"), "w").write("tests/test_a.py\n"); open(os.path.join(vd, "verify.sh"), "w").write(ev.MINED_VERIFY_SH)
    p = subprocess.run(["uv", "run", "--python", "3.12", "--with", "pytest", "bash", os.path.join(vd, "verify.sh"), ws], capture_output=True, text=True, timeout=120)
    assert p.stdout.strip().splitlines()[-1] == "SCORE 0.3333", p.stdout + p.stderr          # 1 passed of 3: the failing one and the skipped one count against it, the conftest did nothing


def _verify(tmp_files, hidden, tests_txt, trusted=None, ws_files=None):
    """Run the mined verify script on a workspace; returns stdout."""
    d = tempfile.mkdtemp(); ws, vd = os.path.join(d, "ws"), os.path.join(d, "verify"); os.makedirs(ws); os.makedirs(vd)
    for rel, c in (ws_files or tmp_files).items():
        os.makedirs(os.path.dirname(os.path.join(ws, rel)) or ws, exist_ok=True); open(os.path.join(ws, rel), "w").write(c)
    for rel, c in hidden.items():
        os.makedirs(os.path.dirname(os.path.join(vd, rel)), exist_ok=True); open(os.path.join(vd, rel), "w").write(c)
    for rel, c in (trusted or {}).items():
        os.makedirs(os.path.dirname(os.path.join(vd, "_trusted", rel)) or vd, exist_ok=True); open(os.path.join(vd, "_trusted", rel), "w").write(c)
    open(os.path.join(vd, "tests.txt"), "w").write(tests_txt); open(os.path.join(vd, "verify.sh"), "w").write(ev.MINED_VERIFY_SH)
    p = subprocess.run(["uv", "run", "--python", "3.12", "--with", "pytest", "bash", os.path.join(vd, "verify.sh"), ws], capture_output=True, text=True, timeout=180)
    return p.stdout + p.stderr


def test_the_graded_run_keeps_the_repos_own_fixtures_and_ignores_the_agents_attempts_to_steer_it():
    if not shutil.which("uv"): return
    base = {"pkg/__init__.py": "", "pkg/mod.py": "def f(): return 1\n"}
    hidden = {"tests/test_a.py": "import pytest\nfrom pkg.mod import f\ndef test_1(shared): assert f() == shared\ndef test_2(): assert f() == 2\n"}
    # the repo's own conftest (a fixture the hidden test needs) comes from the task, so a correct tree is not zeroed
    out = _verify(base, hidden, "tests/test_a.py\n", trusted={"conftest.py": "import pytest\n@pytest.fixture\ndef shared(): return 1\n"}, ws_files=base)
    assert out.strip().splitlines()[-1] == "SCORE 0.5000".replace("0.5000", "0.5"), out
    # the agent adds a plugin through pyproject, a conftest that skips everything, a forged summary printed from its own code, and a directory named like pytest
    cheat = {**base, "pyproject.toml": "[tool.pytest.ini_options]\naddopts='-p evil'\n", "evil.py": "def pytest_runtest_logreport(report): report.outcome='passed'\n", "conftest.py": "import pytest\n",
             "pkg/mod.py": "print('PASSED tests/test_a.py::test_2')\ndef f(): return 1\n", "pytest/__init__.py": "raise SystemExit(0)\n"}
    out = _verify(base, hidden, "tests/test_a.py\n", trusted={"conftest.py": "import pytest\n@pytest.fixture\ndef shared(): return 1\n"}, ws_files=cheat)
    assert out.strip().splitlines()[-1] == "SCORE 0.5", out
    # file names with spaces
    out = _verify(base, {"tests/test one.py": "from pkg.mod import f\ndef test_x(): assert f() == 1\n"}, "tests/test one.py\n", ws_files=base)
    assert out.strip().splitlines()[-1] == "SCORE 1.0" and "SUBTASK tests/test one.py 1.0" in out, out


def test_a_bad_archive_does_not_cost_an_existing_task_its_directory_and_scripts_keep_their_exec_bit():
    import base64, subprocess, io, tarfile
    st = tempfile.mkdtemp(); cfg = ev.cfg_from_env(); cfg["state"] = st; cfg["model_proxy"] = ""
    e = ev.Evaluator(cfg, backend=ev.LocalBackend()); e.submit_batch = lambda body: "b"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in (("repo/run.sh", b"#!/bin/sh\necho\n", 0o755), ("repo/pkg/mod.py", b"x=1\n", 0o644)):
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = mode; tf.addfile(ti, io.BytesIO(data))
    body = {"id": "t-1", "prompt": "p", "init_tar_gz_b64": base64.b64encode(buf.getvalue()).decode(), "verify_files": {"tests/test_a.py": "def test_f(): pass\n"}, "reference_files": {}}
    e.import_task("user-x", body); td = os.path.join(st, "tasksets", "user-x", "t-1")
    assert os.access(os.path.join(td, "init", "run.sh"), os.X_OK) and not os.access(os.path.join(td, "init", "pkg", "mod.py"), os.X_OK)
    try: e.import_task("user-x", {**body, "init_tar_gz_b64": base64.b64encode(b"not an archive").decode()}); raise AssertionError("accepted junk")
    except ValueError: pass
    assert os.path.exists(os.path.join(td, "meta.json")) and e.taskset("user-x")["tasks"][0].id == "t-1"         # the task is still whole


def test_a_task_that_failed_its_own_check_is_left_out_of_scoring_and_unchecked_ones_still_run():
    from evaluator import Evaluator
    class T:
        def __init__(self, id, status): self.id, self.status = id, status
    kept, skipped = Evaluator.scorable([T("a", "ok"), T("b", "invalid"), T("c", "unchecked")])
    assert [t.id for t in kept] == ["a", "c"] and skipped == ["b"]


def test_doing_nothing_on_a_task_with_mostly_passing_files_scores_zero_not_most_of_the_credit():
    noop = {"a.py": 1.0, "b.py": 1.0, "c.py": 0.5, "d.py": 0.0}
    assert ev.lift_score(0.625, dict(noop), noop) == 0.0                                                        # the raw mean of the untouched tree is 0.625
    assert ev.lift_score(1.0, {"a.py": 1.0, "b.py": 1.0, "c.py": 1.0, "d.py": 1.0}, noop) == 1.0
    assert ev.lift_score(0.8, {"a.py": 1.0, "b.py": 1.0, "c.py": 1.0, "d.py": 0.0}, noop) == 0.5                # one of two targets fixed
    assert ev.lift_score(0.7, {"a.py": 0.0, "b.py": 1.0, "c.py": 1.0, "d.py": 1.0}, noop) == 0.5                # all fixed, but one working file broken: its worth comes off
    assert ev.lift_score(0.1, {"a.py": 0.0, "b.py": 0.0, "c.py": 0.0, "d.py": 0.0}, noop) == 0.0              # never below zero


def test_lift_changes_nothing_where_there_is_nothing_to_adjust():
    assert ev.lift_score(0.4, {"a.py": 0.4}, None) == 0.4 and ev.lift_score(0.4, None, {"a.py": 0.0}) == 0.4      # no per-file scores
    assert ev.lift_score(1.0, {"a.py": 1.0}, {"a.py": 1.0}) == 1.0                                              # every file passed before: the check rejects such a task; the score is left alone
    assert ev.lift_score(0.5, {"a.py": 0.5}, {"a.py": 0.0}) == 0.5                                              # one failing file: lift equals the raw score


def test_a_task_set_changes_version_when_a_task_is_rewritten_under_the_same_id():
    import base64, io, tarfile
    st = tempfile.mkdtemp(); cfg = ev.cfg_from_env(); cfg["state"] = st; cfg["model_proxy"] = ""
    e = ev.Evaluator(cfg, backend=ev.LocalBackend()); e.submit_batch = lambda body: "b-check"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"def f():\n    return 1\n"; ti = tarfile.TarInfo("repo/pkg/mod.py"); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    body = {"id": "t-1", "prompt": "do it", "init_tar_gz_b64": base64.b64encode(buf.getvalue()).decode(), "verify_files": {"tests/test_mod.py": "def test_f(): pass\n"}, "reference_files": {}}
    e.import_task("user-o-r", body); a = e.taskset("user-o-r")["version"]
    e.import_task("user-o-r", body); assert e.taskset("user-o-r")["version"] == a                                   # the same task again: the same version
    e.import_task("user-o-r", {**body, "prompt": "do it, and these names exist: f"}); b = e.taskset("user-o-r")["version"]
    assert b != a and len(e.taskset("user-o-r")["tasks"]) == 1                                                     # the same ids, a different prompt: a different measurement
    e.import_task("user-o-r", {**body, "prompt": "do it, and these names exist: f", "verify_files": {"tests/test_mod.py": "def test_f(): assert True\n"}})
    assert e.taskset("user-o-r")["version"] != b
