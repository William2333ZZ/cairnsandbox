"""evolve.py pieces that need no services: the diff critic and Thompson parent selection (docs/007)."""
import json, os, random, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import evolve as E  # noqa: E402


def diff(new_text): return E.unified_diff({"a": b""}, {"a": (new_text + "\n").encode()})


def test_critic_rejects_verification_paths_and_task_names_but_not_common_words():
    ids = ["py-grep", "py-affine-cipher", "py-connect"]
    assert E.critic(diff("use grep to find the symbol; connect to the proxy; run the tests"), ids)[0]
    assert not E.critic(diff("cat /verify/verify.sh"), ids)[0]
    assert "names task py-affine-cipher" in E.critic(diff("solve affine cipher first"), ids)[1]
    assert "names task py-connect" in E.critic(diff("the py-connect task"), ids)[1]
    assert "tamper" in " ".join(E.critic(diff("def charge(): return {'over': False}  # budget_charge"), ids)[1])


def test_critic_size_limit():
    big = "\n".join(f"line {i}" for i in range(450))
    ok, reasons, changed = E.critic(diff(big), [])
    assert not ok and changed > 400 and any("too large" in r for r in reasons)


def _evolve(nodes):
    d = tempfile.mkdtemp(); cfg = E.cfg_from_env(); cfg["state"] = d; cfg["model_proxy"] = ""
    e = E.Evolve(cfg); e.state["nodes"] = nodes; return e


def test_choose_parent_prefers_the_clade_with_accepted_offspring():
    nodes = [
        {"version": "seed", "parent": None, "note": "seed", "accepted": None, "created": 1},
        {"version": "a", "parent": "seed", "note": "", "accepted": True, "created": 2},
        {"version": "a1", "parent": "a", "note": "", "accepted": True, "created": 3},
        {"version": "a2", "parent": "a", "note": "", "accepted": True, "created": 4},
        {"version": "b", "parent": "seed", "note": "", "accepted": False, "created": 5},
        {"version": "c", "parent": "seed", "note": "", "accepted": False, "created": 6},
    ]
    e = _evolve(nodes); rng = random.Random(0)
    picks = [e.choose_parent(rng)["version"] for _ in range(300)]
    # "a" has an all-accepted clade and 2 children; the seed has 1 accepted and 2 rejected children and 3 children in all
    assert picks.count("a") > picks.count("seed")
    assert set(picks) <= {"seed", "a", "a1", "a2"}          # rejected nodes are never parents


def test_new_seed_supersedes_the_old_one_and_its_clade():
    e = _evolve([{"version": "s1", "parent": None, "note": "seed", "accepted": None, "created": 1},
                 {"version": "s1a", "parent": "s1", "note": "", "accepted": True, "created": 2}])
    e.add_node("s2", None, note="seed")
    e.state["nodes"].append({"version": "s2a", "parent": "s2", "note": "", "accepted": True, "created": 4})
    assert {n["version"] for n in e.eligible()} == {"s2", "s2a"}
    assert e.best()["version"] in ("s2", "s2a")
    rng = random.Random(1)
    assert all(e.choose_parent(rng)["version"] in ("s2", "s2a") for _ in range(50))


def test_choose_parent_single_eligible_is_best():
    e = _evolve([{"version": "seed", "parent": None, "note": "seed", "accepted": None, "created": 1}, {"version": "x", "parent": "seed", "note": "", "accepted": False, "created": 2}])
    assert e.choose_parent()["version"] == "seed" and e.best()["version"] == "seed"


def test_update_payload_carries_layers_change_and_numbers():
    from evolve import update_payload
    p = update_payload("v-1", {"label": "cand", "layers": ["L-a", "L-b"]}, {"parent": "v-0", "changes": "fold observations"}, {"mean": 0.7, "delta": 0.05}, "polyglot-py", 0.02, 0.027, {"mean": 0.8, "paired": {"ci": [0.01, 0.2]}})
    assert p["layers"] == ["L-a", "L-b"] and p["changes"] == "fold observations" and p["stats"] == {"mean": 0.8, "parent_mean": 0.7, "delta": 0.05, "ci": [0.01, 0.2], "cost": 0.02, "parent_cost": 0.027}


def test_manifest_keeps_only_what_the_meta_agent_could_know():
    m = E.parse_manifest(json_text := '{"changes": [{"component": "observation", "files": ["aios/model.py", "aios/ghost.py"], "why": "truncated output hid the failing assertion", "evidence": ["py-a", "py-zzz"],'
                         ' "predicts": {"fixes": ["py-a", "py-b", "py-nope"], "regresses": ["py-c"]}, "constraint": "exit codes unchanged"}, {"component": "magic"}]}', ["py-a", "py-b", "py-c"], {"aios/model.py"})
    c = m["changes"][0]
    assert c["files"] == ["aios/model.py"] and c["evidence"] == ["py-a"] and c["predicts"] == {"fixes": ["py-a", "py-b"], "regresses": ["py-c"]} and m["changes"][1]["component"] == "config"
    assert any("py-nope" in d for d in m["dropped"]) and any("py-zzz" in d for d in m["dropped"])
    assert E.parse_manifest("not json", ["py-a"], set()) == {"changes": [], "error": "MANIFEST.json missing or not JSON"} and E.parse_manifest("", [], set())["changes"] == []


def test_attribution_sets_the_prediction_against_the_outcome_and_finds_lost_stable_passes():
    man = {"changes": [{"predicts": {"fixes": ["a", "b", "c"], "regresses": ["d"]}}]}
    base = {t: {"p_hat": p, "pass_k": p == 1.0} for t, p in {"a": 0.0, "b": 0.0, "c": 0.5, "d": 1.0, "e": 1.0, "f": 1.0}.items()}
    cand = {t: {"p_hat": p, "pass_k": p == 1.0} for t, p in {"a": 1.0, "b": 0.0, "c": 0.5, "d": 0.33, "e": 0.67, "f": 1.0}.items()}
    a = E.attribute(man, cand, base)
    assert a["fixed"] == ["a"] and a["hit_fixes"] == ["a"] and a["fix_precision"] == 1 / 3 and a["fix_recall"] == 1.0
    assert a["broke"] == ["d", "e"] and a["hit_regresses"] == ["d"] and a["unpredicted_broke"] == ["e"]            # e broke and nobody saw it coming
    assert a["lost_stable"] == ["d", "e"] and a["solved_by_parent"] == 3 and a["n_tasks"] == 6
    assert E.attribute(None, cand, base)["predicted_fixes"] == [] and E.attribute(man, {}, {})["n_tasks"] == 0
    txt = E.explain({"changes": "head/tail truncation", "manifest": {"changes": [{"why": "hid the assertion"}]}, "attribution": a})
    assert "原因:hid the assertion" in txt and "预计修好 3 道,实际修好 1 道(预计中了 1 道)" in txt and "没预计到的 1 道" in txt


def test_update_payload_carries_why_the_attribution_and_the_kinds():
    node = {"parent": "v0", "changes": "x", "manifest": {"changes": [{"why": "hid the assertion"}]}, "attribution": {"fixed": ["a"], "broke": [], "lost_stable": [], "predicted_fixes": ["a", "b"], "hit_fixes": ["a"], "unpredicted_broke": [], "solved_by_parent": 3, "n_tasks": 6}}
    p = E.update_payload("v1", {"label": "L", "layers": ["l"]}, node, {"mean": 0.5, "delta": 0.05}, "ts", 0.02, 0.03, {"mean": 0.6}, {"kinds": {"cand": {"plain": {"mean": 0.7}}, "parent": {"plain": {"mean": 0.6}}}, "ask": None})
    assert p["why"] == "hid the assertion" and p["attribution"]["fixed"] == 1 and p["attribution"]["predicted_fixes"] == 2 and p["kinds"]["cand"]["plain"]["mean"] == 0.7 and "ask" not in p


def test_critic_also_rejects_removing_the_budget_charge_or_the_report():
    removal = "--- a/aios/agent.py\n+++ b/aios/agent.py\n@@ -1,2 +1,1 @@\n-            r = self.abi.charge(ex.get('tokens_in', 0))\n+            pass\n"
    ok, reasons, _ = E.critic(removal, ["py-a"]); assert not ok and any("removes measurement" in r for r in reasons)
    assert E.critic("--- a/aios/config.json\n+++ b/aios/config.json\n@@ -1 +1 @@\n-old prompt\n+new prompt\n", ["py-a"])[0]


def test_the_same_score_for_much_less_money_is_an_improvement_and_nothing_else_is_let_in_by_that_door():
    st = lambda d: {"paired": {"diff": d, "ci95": [-0.1, 0.1]}}
    assert E.pareto_ok(st(0.0), 0.30, 0.50) and E.pareto_ok(st(0.04), 0.37, 0.50)
    assert not E.pareto_ok(st(-0.02), 0.20, 0.50)                      # cheaper but worse
    assert not E.pareto_ok(st(0.0), 0.40, 0.50)                        # not cheap enough
    assert not E.pareto_ok({"paired": {"diff": None}}, 0.1, 0.5) and not E.pareto_ok(st(0.0), 0.001, 0.005)      # no pairing, or a base too cheap for 'cheaper' to mean anything


def test_a_manifest_with_the_wrong_shapes_is_read_not_crashed_on_and_the_efficiency_arm_needs_an_interval():
    m = E.parse_manifest('{"changes": [{"component": "loop", "files": 5, "evidence": "py-a", "predicts": {"fixes": 3, "regresses": ["py-a", 7]}}, "x", 4]}', ["py-a"], {"aios/model.py"})
    assert m["changes"][0]["files"] == [] and m["changes"][0]["evidence"] == [] and m["changes"][0]["predicts"] == {"fixes": [], "regresses": ["py-a"]}
    assert E.parse_manifest('{"changes": 5}', [], set())["changes"] == [] and E.parse_manifest('[1,2]', [], set())["changes"] == []
    wide = {"paired": {"diff": 0.0, "ci95": [-0.4, 0.4]}}; tight = {"paired": {"diff": 0.0, "ci95": [-0.05, 0.05]}}
    assert not E.pareto_ok(wide, 0.2, 0.5) and E.pareto_ok(tight, 0.2, 0.5)                              # cheaper by luck is not cheaper


def test_a_named_candidate_is_the_whole_harness_with_one_explained_change_and_goes_through_the_manifest_parser():
    import json, tempfile
    import candidates as C
    out = C.build("nudge", tempfile.mkdtemp(), os.path.join(os.path.dirname(__file__), "..", "..", "aios"), ["h-b4e485dc"])
    cfg = json.load(open(os.path.join(out, "aios", "config.json")))
    assert cfg["agent"]["middleware"] == [{"name": "progress_nudge", "at": 15}] and os.path.exists(os.path.join(out, "aios", "agent.py")) and not os.path.exists(os.path.join(out, "aios", "__pycache__"))
    m = E.parse_manifest(open(os.path.join(out, "MANIFEST.json")).read(), ["h-b4e485dc"], ["aios/config.json"])
    assert m["changes"][0]["component"] == "middleware" and m["changes"][0]["predicts"]["fixes"] == ["h-b4e485dc"] and not m.get("dropped")
    a = C.build("ask-paragraph", tempfile.mkdtemp(), os.path.join(os.path.dirname(__file__), "..", "..", "aios"), [])
    assert "Missing decisions" in json.load(open(os.path.join(a, "aios", "config.json")))["agent"]["instance_template"]


def test_a_baseline_or_compare_from_another_scoring_or_task_set_is_not_reused_and_gets_no_verdict():
    import evaluator as EV
    assert E.SCORING == EV.SCORING_VERSION
    class Fake(E.Evolve):
        def __init__(self): self.state = {"nodes": [{"version": "v1", "baseline": {"taskset": "T", "k": 2, "status": "done", "scoring": "1", "taskset_version": "aaaa", "batch": "b-old"}}], "iterations": [], "spent_usd": 0}; self.calls = []
        def ev(self, method, path, body=None):
            self.calls.append((method, path))
            if path == "/tasksets/T": return {"version": "aaaa"}
            if path == "/batches" and method == "POST": return {"batch_id": "b-new"}
            return {"status": "done", "stats": {"mean": 0.5}, "matrix": {}, "params": {"scoring": E.SCORING, "taskset_version": "aaaa"}}
        def save(self): pass
    f = Fake(); b = f.baseline("v1", "T", 2)
    assert b["batch"] == "b-new" and b["scoring"] == E.SCORING                       # the stored one was scored the old way: measured again
    assert f.baseline("v1", "T", 2)["batch"] == "b-new" and len([c for c in f.calls if c == ("POST", "/batches")]) == 1      # ... and then reused


def test_old_component_names_in_a_manifest_are_read_as_the_new_ones():
    t = '{"changes": [{"component": "loop"}, {"component": "prompt"}, {"component": "observation"}, {"component": "tools"}, {"component": "memory"}, {"component": "whatever"}]}'
    assert [c["component"] for c in E.parse_manifest(t, [], [])["changes"]] == ["middleware", "system_prompt", "tool_impl", "tool_description", "memory", "config"]


def test_an_accepted_candidate_becomes_a_home_proposal_that_apply_moves_in_and_code_changes_stay_with_the_layer(tmp_path):
    """D29: evolve writes only under proposals/; the overlay is the smallest config diff; code outside the home layout is only listed."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from aios.home import Home
    parent = {"aios/config.json": json.dumps({"agent": {"max_steps": 40, "middleware": [{"name": "compact_history"}]}, "model": {"name": "x"}}).encode(),
              "aios/agent.py": b"print(1)\n", "aios/prompts/system.md": b"old system", "aios/rules/01.md": b"keep", "aios/rules/02.md": b"drop", "aios/memory.md": b"notes"}
    cand = dict(parent); cand["aios/config.json"] = json.dumps({"agent": {"max_steps": 60, "middleware": [{"name": "compact_history"}, {"name": "progress_nudge", "at": 15}]}, "model": {"name": "x"}}).encode()
    cand["aios/agent.py"] = b"print(2)\n"; cand["aios/prompts/system.md"] = b"new system {{task}}"; cand["aios/skills/s/SKILL.md"] = b"---\nname: s\ndescription: d\n---\nbody"; del cand["aios/rules/02.md"]
    files, deletes, code = E.home_proposal(parent, cand)
    assert json.loads(files["config.json"]) == {"agent": {"max_steps": 60, "middleware": [{"name": "compact_history"}, {"name": "progress_nudge", "at": 15}]}}       # a list is replaced whole, an unchanged subtree is absent
    assert files["prompts/system.md"] == b"new system {{task}}" and files["skills/s/SKILL.md"].startswith(b"---") and "memory.md" not in files
    assert deletes == ["rules/02.md"] and code == ["aios/agent.py"]
    assert E.config_overlay({"a": {"b": 1, "c": 2}}, {"a": {"b": 1}}) == {"a": {"_removed": ["c"]}}
    home = tmp_path / "home"; h = Home(home); h.init(); (home / "rules").mkdir(exist_ok=True); (home / "rules" / "02.md").write_text("drop")
    it = {"id": "it-1", "parent": "h-p", "taskset": "ts", "k": 2, "result": "accepted: mean 0.9 vs parent 0.8", "changes": "longer runs", "layer": "L-1"}
    node = {"accepted": True, "compare": {"mean": 0.9, "cost_per_run": 0.01, "parent_cost_per_run": 0.01, "batch": "b1"}}
    d = E.write_proposal(str(home), "h-c", files, deletes, E.proposal_text("h-c", it, node, files, deletes, code))
    assert h.proposals() == ["h-c"] and "aios/agent.py" in open(os.path.join(d, "PROPOSAL.md"), encoding="utf-8").read() and h.config() == {}
    r = h.apply("h-c")                                                            # PROPOSAL.md and DELETE are not moved, everything else is, the removal happens
    assert h.config()["agent"]["max_steps"] == 60 and sorted(r["files"]) == ["config.json", "prompts/system.md", "skills/s/SKILL.md"] and r["deleted"] == ["rules/02.md"]
    assert not (home / "rules" / "02.md").exists() and not (home / "PROPOSAL.md").exists() and h.proposals() == [] and h.check() == []
    import pytest
    with pytest.raises(FileExistsError): E.write_proposal(str(home), "h-c", {}, [], "x") if (home / "proposals" / "h-c").mkdir() is None else None
