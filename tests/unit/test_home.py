"""The personal harness directory (aios/home.py, docs/004, D29): what a person puts there reaches the next run as it is; what the machine proposes waits under proposals/
until it is applied; a home that cannot deliver what it claims stops the run instead of running without it."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from aios import home as H
from aios import run as R
from test_aios_seed import FakeModel, CFG


def home(tmp_path, **files):
    d = tmp_path / "home"; d.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = d / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text if isinstance(text, str) else json.dumps(text))
    return H.Home(d)


def test_no_home_is_no_change(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_HOME", raising=False); monkeypatch.setattr(H, "DEFAULT", str(tmp_path / "absent"))
    assert H.find() is None
    monkeypatch.setenv("CAIRN_HOME", str(tmp_path / "h")); assert H.find().path == tmp_path / "h"
    assert H.find(str(tmp_path / "x")).path == tmp_path / "x"                                     # --home beats the environment
    assert H.Home(tmp_path / "absent").check() == [f"{tmp_path / 'absent'} is not a directory"]
    h = home(tmp_path); assert h.check() == [] and h.task_text() == "" and h.apply_to(CFG) == CFG and h.rules() == [] and h.skills() == []


def test_init_makes_a_usable_empty_home_whose_readme_and_comments_add_nothing(tmp_path):
    h = H.Home(tmp_path / "h"); h.init()
    assert h.check() == [] and h.rules() == [] and R.harness_memory(h.memory_path()) == "" and h.task_text() == ""
    d0 = h.digest(); (h.path / "rules" / "10-tests.md").write_text("<!-- why -->\nRun the narrowest test first.\n")
    assert h.digest() != d0 and h.rules() == ["Run the narrowest test first."]
    (h.path / "tasks" / "repo" / "t1").mkdir(parents=True); (h.path / "tasks" / "repo" / "t1" / "prompt.md").write_text("x")
    (h.path / "proposals" / "p1").mkdir(parents=True); (h.path / "proposals" / "p1" / "memory.md").write_text("machine notes")
    assert h.digest() == H.Home(h.path).digest() and "tasks/repo/t1/prompt.md" not in h.files() and not any(f.startswith("proposals") for f in h.files())   # the runtime reads neither


def test_rules_in_filename_order_comments_out_and_only_md(tmp_path):
    h = home(tmp_path, **{"rules/20-b.md": "Second rule.", "rules/10-a.md": "<!-- note -->First\nrule.", "rules/notes.txt": "not a rule", "rules/30-empty.md": "<!-- only a comment -->"})
    assert h.rules() == ["First rule.", "Second rule."]
    assert h.check() == ["rules/notes.txt: only .md files are read"]
    text = h.task_text(); assert "standing rules" in text and "- First rule.\n- Second rule." in text and "Skills" not in text


def test_config_merges_dicts_and_replaces_lists_and_flags_unknown_keys(tmp_path):
    h = home(tmp_path, **{"config.json": {"agent": {"step_limit": 12, "middleware": ["compact_history"]}, "model": {"model_kwargs": {"temperature": 0}}}})
    cfg = h.apply_to(CFG)
    assert cfg["agent"]["step_limit"] == 12 and cfg["agent"]["middleware"] == ["compact_history"] and cfg["agent"]["system_template"] == CFG["agent"]["system_template"]
    assert cfg["model"]["model_kwargs"] == {"max_tokens": 4096, "temperature": 0} and cfg["environment"] == CFG["environment"]
    assert home(tmp_path, **{"config.json": {"agnet": {}}}).check() == ["config.json: unknown top-level key 'agnet' (agent, environment, model)"]
    assert home(tmp_path, **{"config.json": "{nope"}).check()[0].startswith("config.json:")
    assert home(tmp_path, **{"config.json": "[1]"}).check() == ["config.json: config.json must hold an object"]


def test_prompts_replace_the_templates_and_an_instance_without_the_task_is_refused(tmp_path):
    h = home(tmp_path, **{"prompts/system.md": "You are careful.\n", "prompts/instance.md": "Do this: {{task}}\nThen stop.", "prompts/communication.md": "<!-- c -->Say when done."})
    cfg = h.apply_to(CFG); assert cfg["agent"]["system_template"] == "You are careful.\n" and cfg["agent"]["instance_template"].startswith("Do this: {{task}}")
    assert h.prompts()["communication"] == "Say when done." and h.check() == []
    assert home(tmp_path, **{"prompts/instance.md": "no task here"}).check() == ["prompts/instance.md must contain {{task}}"]
    assert home(tmp_path / "b", **{"prompts/scope.md": "x" * 3001}).check() == ["prompts/scope.md is over 3000 characters"]


def test_skills_are_listed_by_name_and_description_and_read_by_path(tmp_path):
    h = home(tmp_path, **{"skills/release/SKILL.md": "---\nname: release\ndescription: how we cut a release\n---\n# Steps\n1. tag\n",
                          "skills/bare/SKILL.md": "# Bare skill\nUse when nothing else fits.\n"})
    s = h.skills(); assert [x["name"] for x in s] == ["bare", "release"] and s[1]["description"] == "how we cut a release" and s[0]["description"] == "Bare skill"
    assert s[1]["path"].endswith("skills/release/SKILL.md")
    text = h.task_text(); assert "release: how we cut a release (" in text and "read the file before you use one" in text
    (h.path / "skills" / "broken").mkdir(); assert h.check() == ["skills/broken/ has no SKILL.md"]
    (h.path / "skills" / "broken" / "SKILL.md").write_text("---\nname: release\n---\n"); assert h.check() == ["skills/: two skills share a name"]


def test_the_homes_own_middleware_loads_and_a_broken_spec_fails_the_check(tmp_path):
    h = home(tmp_path, **{"middleware/mine.py": "from aios.middleware import Middleware\nclass Tag(Middleware):\n    name='tag'\n    def __init__(self, text='t'): self.text=text\n    def before_step(self, agent): agent.extra_template_vars['tag']=self.text\n",
                          "config.json": {"agent": {"middleware": [{"class": "mine:Tag", "text": "hello"}]}}})
    assert h.check() == []
    (h.path / "config.json").write_text(json.dumps({"agent": {"middleware": [{"class": "mine:Nope"}]}}))
    assert h.check() == ["agent.middleware: AttributeError: module 'mine' has no attribute 'Nope'"]
    (h.path / "config.json").write_text(json.dumps({"agent": {"middleware": ["no_such"]}}))
    assert h.check()[0].startswith("agent.middleware: ValueError: unknown middleware 'no_such'")
    assert str(h.middleware_dir()) not in sys.path                                                 # the check leaves the import path as it found it


def test_a_run_with_a_home_carries_its_rules_notes_prompts_and_digest(tmp_path, monkeypatch):
    h = home(tmp_path, **{"rules/01.md": "Never touch CI files.", "memory.md": "<!-- c -->\nThe test runner is pytest.", "prompts/system.md": "SYSTEM FROM HOME",
                          "config.json": {"agent": {"step_limit": 7}}})
    monkeypatch.setattr(R, "Model", lambda **kw: FakeModel(["echo hi"], **{k: v for k, v in kw.items() if k not in ("model_name", "base_url", "api_key")}))
    out = tmp_path / "traj.json"; work = tmp_path / "w"; work.mkdir()
    rc = R.main(["--task", "do the thing", "--out", str(out), "--cwd", str(work), "--home", str(h.path), "--config", os.path.join(os.path.dirname(R.__file__), "config.json")])
    t = json.loads(out.read_text()); assert rc == 0
    assert t["messages"][0]["content"] == "SYSTEM FROM HOME"
    task = t["messages"][1]["content"]; assert "do the thing" in task and "- Never touch CI files." in task and "The test runner is pytest." in task and task.index("Never touch") < task.index("pytest")
    ac = t["info"]["config"]["agent"]; assert ac["step_limit"] == 7 and ac["harness_home"] == str(h.path) and ac["harness_digest"] == h.digest()
    (h.path / "prompts" / "instance.md").write_text("broken, no task")                             # a home that fails its check stops the run before any model call
    rc = R.main(["--task", "x", "--out", str(tmp_path / "t2.json"), "--cwd", str(work), "--home", str(h.path)]); assert rc == 1 and not (tmp_path / "t2.json").exists()


def test_a_run_without_a_home_records_none_and_uses_the_package_notes(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_HOME", raising=False); monkeypatch.setattr(H, "DEFAULT", str(tmp_path / "absent"))
    monkeypatch.setattr(R, "Model", lambda **kw: FakeModel([], **{k: v for k, v in kw.items() if k not in ("model_name", "base_url", "api_key")}))
    out = tmp_path / "traj.json"; work = tmp_path / "w"; work.mkdir()
    assert R.main(["--task", "t", "--out", str(out), "--cwd", str(work)]) == 0
    ac = json.loads(out.read_text())["info"]["config"]["agent"]; assert ac["harness_home"] == "" and ac["harness_digest"] == ""


def test_a_proposal_reaches_the_run_only_when_applied_and_a_bad_one_is_put_back(tmp_path):
    h = home(tmp_path, **{"memory.md": "old notes", "rules/01.md": "Keep.", "rules/02.md": "Drop me."}); h.init(); d0 = h.digest()
    (h.path / "proposals" / "p1").mkdir(parents=True); (h.path / "proposals" / "p1" / "memory.md").write_text("new notes")
    (h.path / "proposals" / "p1" / "rules").mkdir(); (h.path / "proposals" / "p1" / "rules" / "03.md").write_text("Added.")
    (h.path / "proposals" / "p1" / "DELETE").write_text("rules/02.md\n")
    assert h.proposals() == ["p1"] and h.digest() == d0 and h.rules() == ["Keep.", "Drop me."]           # proposed, not in effect
    r = h.apply("p1")
    assert r["before"] == d0 and r["after"] == h.digest() != d0 and h.rules() == ["Keep.", "Added."] and (h.path / "memory.md").read_text() == "new notes"
    assert h.proposals() == [] and json.loads((h.path / "history.jsonl").read_text())["proposal"] == "p1"
    (h.path / "proposals" / "p2" / "tasks" / "r").mkdir(parents=True); (h.path / "proposals" / "p2" / "tasks" / "r" / "x").write_text("x")
    with pytest.raises(PermissionError): h.apply("p2")                                            # the machine may not touch the regression tasks (or anything the runtime does not read)
    (h.path / "proposals" / "p3").mkdir(); (h.path / "proposals" / "p3" / "config.json").write_text("{broken")
    d1 = h.digest()
    with pytest.raises(ValueError): h.apply("p3")
    assert h.digest() == d1 and h.check() == [] and (h.path / "proposals" / "p3").is_dir()          # nothing applied, the proposal stays for a person to look at
    with pytest.raises(FileNotFoundError): h.apply("nope")


def test_cli(tmp_path, capsys):
    d = tmp_path / "h"; assert H.main(["init", str(d)]) == 0 and H.main(["check", str(d)]) == 0 and capsys.readouterr().out.strip().splitlines()[-1].startswith("ok h-")
    (d / "prompts" / "instance.md").write_text("nope"); assert H.main(["check", str(d)]) == 1 and "{{task}}" in capsys.readouterr().out
