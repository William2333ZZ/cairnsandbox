"""D24: a mined task tells the agent which names its hidden tests use that the workspace does not have yet."""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import interfaces as I  # noqa: E402
import mine_history as M  # noqa: E402

TEST = ("import os\nfrom pkg.mod import f, g, H\nimport pkg.mod as m\nfrom pkg.fresh import make, Thing\nfrom helper import x\n"
        "def test_a(): assert f() == 1 and m.extra_value and os.sep and make() and Thing\n")


def test_names_the_tests_take_from_a_module_that_it_does_not_define_are_listed_and_a_new_module_is_named():
    init = {"pkg/mod.py": "def f(): return 1\nclass H: pass\n", "helper.py": "x = 1\n"}
    lines = I.missing_interfaces({"tests/test_a.py": TEST}, list(init), init.get, ["pkg/fresh.py", "pkg/other.py"])
    assert lines == ["new file `pkg/fresh.py`: `Thing`, `make`", "`pkg/mod.py` needs: `extra_value`, `g`"]
    assert not any("helper" in l or "os" in l for l in lines)                       # something that already exists, and the standard library, are not asked for


def test_a_syntax_error_in_a_test_or_a_source_asks_for_nothing_instead_of_failing():
    assert I.missing_interfaces({"t.py": "def (:"}, ["a.py"], lambda p: "x = 1", []) == []
    assert I.missing_interfaces({"t.py": "from a import z\n"}, ["a.py"], lambda p: "def (:", []) == []             # an unreadable source is not a reason to list every name


def test_the_hints_reach_the_prompt_of_a_mined_task_and_an_obstacle_variant_does_not_leak_its_decision():
    d = tempfile.mkdtemp()
    def sh(*a): subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    def w(p, s): os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True); open(os.path.join(d, p), "w").write(s)
    sh("init", "-q", "-b", "main"); w("pkg/__init__.py", ""); w("pkg/mod.py", "def f(): return 1\n"); sh("add", "-A"); sh("commit", "-qm", "start")
    w("pkg/mod.py", "def f(): return 1\ndef window_s(): return 30\n"); w("tests/test_mod.py", "from pkg.mod import f, window_s\ndef test_w(): assert window_s() == 30\n"); sh("add", "-A"); sh("commit", "-qm", "add a window")
    c = M.candidates(d)[0]; b = M.task_body(d, c, "evolve")
    assert "`pkg/mod.py` needs: `window_s`" in b["prompt"] and "use exactly these names" in b["prompt"]
    blocker = {"request": "add a window to pkg.mod", "question": "how long?", "answer": "window_s returns 30"}
    assert "window_s" not in M.hints_for(d, c["sha"], {"tests/test_mod.py": "from pkg.mod import f, window_s\n"}, blocker)       # the answer's words are not in the hint


def test_star_re_exports_and_a_packages_own_submodules_are_not_missing_names():
    init = {"intake/intake.py": "from sources import *\n", "aios/__init__.py": "", "aios/env.py": "x = 1\n", "aios/run.py": "y = 1\n"}
    tests = {"t.py": "from intake import CISource\nfrom aios import env, run, nothing_here\n"}
    assert I.missing_interfaces(tests, list(init), init.get, []) == ["`aios/__init__.py` needs: `nothing_here`"]


def test_conditional_definitions_helpers_the_commit_rewrites_and_added_submodules_are_not_missing_names():
    init = {"pkg/__init__.py": "", "pkg/a.py": "try:\n    from .compat import urlparse\nexcept ImportError:\n    urlparse = None\nwith open('x') as f:\n    def g(): pass\n", "tests/helpers.py": "x = 1\n"}
    tests = {"tests/helpers.py": "def build_x(): pass\n", "tests/test_a.py": "from pkg.a import urlparse, g\nfrom helpers import build_x\nfrom pkg import newmod\n"}
    assert I.missing_interfaces(tests, list(init), init.get, ["pkg/newmod.py"]) == []


def test_an_obstacle_hint_is_withheld_when_a_name_could_carry_the_decision_or_the_answer_cannot_be_checked():
    import mine_history as M2
    import tempfile, subprocess
    d = tempfile.mkdtemp()
    def sh(*a): subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    def w(p, s): os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True); open(os.path.join(d, p), "w").write(s)
    sh("init", "-q", "-b", "main"); w("pkg/mod.py", "x = 1\n"); sh("add", "-A"); sh("commit", "-qm", "s")
    w("pkg/mod.py", "x = 1\nRETRY_LIMIT = 3\n"); w("tests/test_m.py", "from pkg.mod import RETRY_LIMIT\n"); sh("add", "-A"); sh("commit", "-qm", "c")
    sha = M2.candidates(d)[0]["sha"]; v = {"tests/test_m.py": "from pkg.mod import RETRY_LIMIT\n"}
    assert "RETRY_LIMIT" in M2.hints_for(d, sha, v, None)
    assert M2.hints_for(d, sha, v, {"answer": "retry limit is 3"}) == ""                  # `RetryLimit`, `retry_limit`, `RETRY_LIMIT`: the same words
    assert M2.hints_for(d, sha, v, {"answer": "三次"}) == ""                              # nothing to compare against: no hints rather than a guess
