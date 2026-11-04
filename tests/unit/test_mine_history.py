"""Tasks from a repository's own history (eval/mine_history.py): which commits qualify, what the task is made of, how the sets are split."""
import base64, io, os, subprocess, sys, tarfile, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import mine_history as M  # noqa: E402


def repo():
    d = tempfile.mkdtemp()
    def sh(*a): subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    def w(p, s): os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True); open(os.path.join(d, p), "w").write(s)
    sh("init", "-q", "-b", "main")
    w("pkg/__init__.py", ""); w("pkg/mod.py", "def f(): return 1\n"); w("README.md", "x\n"); sh("add", "-A"); sh("commit", "-qm", "start")
    w("docs/a.md", "only docs\n"); sh("add", "-A"); sh("commit", "-qm", "docs only")
    w("pkg/mod.py", "def f(): return 2\n"); w("tests/test_mod.py", "from pkg.mod import f\ndef test_f(): assert f() == 2\n"); sh("add", "-A")
    sh("commit", "-qm", "make f return 2\n\nThe callers expect two.\n\nCo-Authored-By: someone <s@s>")
    w("pkg/mod.py", "def f(): return 3\n"); sh("add", "-A"); sh("commit", "-qm", "source without tests")
    w("tests/test_other.py", "def test_x(): pass\n"); sh("add", "-A"); sh("commit", "-qm", "tests without source")
    for i in range(9): w(f"pkg/big{i}.py", "x = 1\n")
    w("tests/test_big.py", "def test_b(): pass\n"); sh("add", "-A"); sh("commit", "-qm", "too many files")
    return d


def test_only_commits_with_python_tests_and_python_source_within_limits_are_tasks():
    d = repo(); cs = M.candidates(d, max_files=8)
    assert [M.message(d, c["sha"])[0] for c in cs] == ["make f return 2"] and cs[0]["tests"] == ["tests/test_mod.py"] and cs[0]["src"] == ["pkg/mod.py"]
    assert len(M.candidates(d, max_files=20)) == 2                       # the big one qualifies when the limit allows it


def test_a_task_is_the_parent_workspace_the_commits_tests_hidden_and_its_source_as_reference():
    d = repo(); c = M.candidates(d)[0]; b = M.task_body(d, c, "evolve")
    assert b["id"] == "h-" + c["sha"][:8] and b["group"] == "evolve" and b["kind"] == "plain" and b["source"]["origin"] == "history"
    assert b["verify_files"] == {"tests/test_mod.py": "from pkg.mod import f\ndef test_f(): assert f() == 2\n"} and b["reference_files"] == {"pkg/mod.py": "def f(): return 2\n"}
    assert b["prompt"].startswith("# make f return 2") and "The callers expect two." in b["prompt"] and "Co-Authored-By" not in b["prompt"] and "hidden tests" in b["prompt"]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b["init_tar_gz_b64"])), mode="r:gz") as tf:
        names = tf.getnames(); data = tf.extractfile("repo/pkg/mod.py").read()
    assert "repo/pkg/mod.py" in names and data == b"def f(): return 1\n" and "repo/tests/test_mod.py" not in names          # the workspace is the repository before the commit


def test_later_work_is_held_out():
    assert M.split_groups(10) == ["evolve"] * 6 + ["validate"] * 2 + ["test"] * 2 and M.split_groups(1) == ["evolve"] and M.split_groups(0) == []


def test_an_obstacle_variant_asks_for_one_decision_that_is_in_the_change_and_not_in_the_request():
    d = repo(); c = M.candidates(d)[0]
    good = '{"request": "让 f 的返回值符合调用方的期望", "question": "f 应该返回几?", "answer": "return 2"}'
    b = M.blocker_for(d, c, lambda msgs: good); assert b == {"request": "让 f 的返回值符合调用方的期望", "question": "f 应该返回几?", "answer": "return 2"}
    assert M.blocker_for(d, c, lambda m: '{"request": ""}') is None and M.blocker_for(d, c, lambda m: "not json") is None
    assert M.blocker_for(d, c, lambda m: '{"request": "x", "question": "q?", "answer": "the colour purple of the bikeshed"}') is None            # not in the change: made up
    assert M.blocker_for(d, c, lambda m: '{"request": "make f return 2 please", "question": "q?", "answer": "return 2"}') is None                 # the request gives it away
    t = M.task_body(d, c, "evolve", b)
    assert t["id"] == "o-" + c["sha"][:8] and t["kind"] == "obstacle" and t["blockers"] == [{"question": "f 应该返回几?", "answer": "return 2"}] and t["source"]["origin"] == "history+synthetic-blocker"
    assert "return 2" not in t["prompt"] and "make f return 2" not in t["prompt"] and t["verify_files"]


def test_a_dependent_variant_hands_the_decision_over_as_a_rule_and_leaves_the_request_without_it():
    d = repo(); c = M.candidates(d)[0]
    answers = iter(['{"request": "让 f 的返回值符合调用方的期望", "question": "f 应该返回几?", "answer": "return 2"}', "f 一律返回 2(调用方都按两来写)。"])
    b = M.dependent_for(d, c, lambda msgs: next(answers)); assert b["rule"] == "f 一律返回 2(调用方都按两来写)。" and b["answer"] == "return 2"
    t = M.task_body(d, c, "evolve", b)
    assert t["id"] == "d-" + c["sha"][:8] and t["kind"] == "dependent" and t["rules"] == [b["rule"]] and "blockers" not in t and t["source"]["origin"] == "history+synthetic-rule"
    assert "return 2" not in t["prompt"] and "一律返回 2" not in t["prompt"]


def test_non_ascii_test_names_are_read_as_they_are():
    d = repo()
    def sh(*a): subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    open(os.path.join(d, "tests", "test_测试.py"), "w").write("def test_x(): pass\n"); open(os.path.join(d, "pkg", "new.py"), "w").write("y = 1\n"); sh("add", "-A"); sh("commit", "-qm", "non-ascii test file")
    c = [x for x in M.candidates(d, max_files=8) if "tests/test_测试.py" in x["tests"]]
    assert c and c[0]["src"] == ["pkg/new.py"]
