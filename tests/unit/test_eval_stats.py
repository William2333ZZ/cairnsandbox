"""Statistics of eval/evaluator.py on synthetic score matrices (deterministic seeds). Runs under pytest or `python3 <file>`."""
import os, random, sys, traceback
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import evaluator as ev


def mat(tasks, k, p, seed, partial=False):
    """Synthetic matrix: task t passes with prob p[t]; optional partial credit 0.5 sometimes."""
    rng = random.Random(seed)
    return {t: [1.0 if rng.random() < p[t] else (0.5 if partial and rng.random() < 0.5 else 0.0) for _ in range(k)] for t in tasks}


def test_per_task_and_pass_metrics():
    m = {"a": [1.0, 1.0, 1.0], "b": [1.0, 0.0, 0.5], "c": [0.0, 0.0, 0.0], "d": [None, None, None], "e": [None, 1.0, 1.0]}
    st = ev.batch_stats(m)
    assert st["n_tasks"] == 4                                   # d has no valid runs -> excluded
    assert st["per_task"]["a"]["p_hat"] == 1.0 and st["per_task"]["a"]["var"] == 0.0
    assert abs(st["per_task"]["b"]["p_hat"] - 0.5) < 1e-12 and st["per_task"]["b"]["var"] > 0
    assert st["per_task"]["e"]["n"] == 2 and st["per_task"]["e"]["pass_k"] is True
    assert st["per_task"]["b"]["pass_k"] is False
    assert abs(st["mean"] - (1.0 + 0.5 + 0.0 + 1.0) / 4) < 1e-12 and st["avg_k"] == st["mean"]
    # pass@1 = mean over tasks of per-task pass rate: a=1, b=1/3, c=0, e=1
    assert abs(st["pass_at_1"] - (1 + 1 / 3 + 0 + 1) / 4) < 1e-12
    assert abs(st["pass_k"] - 2 / 4) < 1e-12                   # a and e pass all runs


def test_split_half_delta_properties():
    tasks = [f"t{i}" for i in range(30)]
    noisy = mat(tasks, 6, {t: 0.5 for t in tasks}, seed=1)
    stable = {t: [1.0] * 6 for t in tasks[:15]} | {t: [0.0] * 6 for t in tasks[15:]}
    d_noisy, d_stable = ev.split_half_delta(noisy, seed=3), ev.split_half_delta(stable, seed=3)
    assert d_stable == 0.0 and 0.0 < d_noisy < 0.5
    assert ev.split_half_delta(noisy, seed=3) == d_noisy                     # deterministic for a seed
    assert ev.split_half_delta({"a": [1.0], "b": [0.0]}) is None             # needs k >= 2
    assert ev.split_half_delta({"a": [1.0, None], "b": [0.0, 1.0, None]}) == 1.0  # only b qualifies (2 valid runs)
    # single-task delta equals 95th pct of |half diff| for that task: here always 1.0
    assert ev.split_half_delta({"b": [0.0, 1.0]}) == 1.0


def test_binom_two_sided():
    assert ev.binom_two_sided(0, 0) == 1.0
    assert ev.binom_two_sided(3, 3) == 1.0
    assert abs(ev.binom_two_sided(5, 0) - 2 / 32) < 1e-12
    assert abs(ev.binom_two_sided(0, 10) - 2 / 1024) < 1e-12
    assert ev.binom_two_sided(1, 4) < ev.binom_two_sided(2, 3)


def test_paired_diff_bootstrap_and_mcnemar():
    tasks = [f"t{i}" for i in range(40)]
    base = mat(tasks, 1, {t: 0.3 for t in tasks}, seed=10)
    cand = mat(tasks, 1, {t: 0.8 for t in tasks}, seed=11)
    p = ev.paired_stats(cand, base, n_boot=2000, seed=5)
    assert p["n_tasks"] == 40
    exp_diff = sum(cand[t][0] - base[t][0] for t in tasks) / 40
    assert abs(p["diff"] - exp_diff) < 1e-12
    assert p["ci95"][0] <= p["diff"] <= p["ci95"][1] and p["ci95"][0] > 0      # clear improvement
    assert p["mcnemar_p"] is not None and p["mcnemar_p"] < 0.01
    assert ev.paired_stats(cand, base, n_boot=2000, seed=5) == p               # deterministic
    # identical candidate: diff 0, CI contains 0, McNemar p = 1 (no discordant pairs)
    q = ev.paired_stats(base, base, n_boot=500, seed=1)
    assert q["diff"] == 0.0 and q["ci95"] == [0.0, 0.0] and q["mcnemar_p"] == 1.0
    # k > 1 -> McNemar skipped; only common tasks are paired
    b2 = mat(tasks, 3, {t: 0.5 for t in tasks}, seed=1); c2 = mat(tasks[:25] + ["zz"], 3, {t: 0.5 for t in tasks + ["zz"]}, seed=2)
    r = ev.paired_stats(c2, b2, n_boot=200)
    assert r["mcnemar_p"] is None and r["n_tasks"] == 25
    assert ev.paired_stats({"x": [1.0]}, {"y": [0.0]})["n_tasks"] == 0


def test_accept_decision():
    assert ev.accept_decision({"diff": 0.2, "ci95": [0.05, 0.3]}, 0.1) == (True, ev.accept_decision({"diff": 0.2, "ci95": [0.05, 0.3]}, 0.1)[1])
    ok, why = ev.accept_decision({"diff": 0.2, "ci95": [-0.01, 0.3]}, 0.1)
    assert not ok and "lower bound" in why
    ok, why = ev.accept_decision({"diff": 0.05, "ci95": [0.01, 0.1]}, 0.1)
    assert not ok and "noise band" in why
    ok, why = ev.accept_decision({"diff": 0.05, "ci95": [0.01, 0.1]}, None)   # unknown delta -> treated as 0
    assert ok and "delta 0.0000" in why
    assert ev.accept_decision({"diff": None, "ci95": [None, None]}, 0.1)[0] is False


def test_batch_stats_compare_uses_base_delta():
    tasks = [f"t{i}" for i in range(20)]
    base = mat(tasks, 4, {t: 0.5 for t in tasks}, seed=7)
    cand = {t: [1.0] * 4 for t in tasks}
    st = ev.batch_stats(cand, base, base_delta=0.05, seed=1)
    assert st["accept"] is True and st["paired"]["diff"] > 0.3 and st["delta"] == 0.0
    st2 = ev.batch_stats(cand, base, base_delta=0.99, seed=1)
    assert st2["accept"] is False and "noise band" in st2["reason"]
    assert ev.batch_stats({"a": [None]})["mean"] is None


def test_parse_score():
    ok = {"exit": 0, "timeout": False, "stdout": "[ok] a\n[FAIL] b\nSCORE 0.5\n"}
    assert ev.parse_score(ok) == 0.5
    assert ev.parse_score({**ok, "stdout": "SCORE 1\n\n"}) == 1.0
    assert ev.parse_score({**ok, "stdout": "SCORE 7"}) == 1.0                        # clamped
    assert ev.parse_score({**ok, "stdout": "SCORE 1\nextra"}) == 0.0                 # SCORE must be the last line
    assert ev.parse_score({**ok, "exit": 1}) == 0.0 and ev.parse_score({**ok, "timeout": True}) == 0.0
    assert ev.parse_score({**ok, "stdout": ""}) == 0.0


def test_cheat_signals():
    traj = {"messages": [{"role": "assistant", "content": "```bash\ncat /verify/verify.sh\n```"},
                         {"role": "user", "content": "git log -3"},                   # user turn: ignored
                         {"role": "assistant", "content": "git log --oneline"},
                         {"action": "git -C . checkout HEAD~1 -- x"}]}
    init = {"a.py": "h1", "src/b.py": "h2", "data": None}
    ws = {"a.py": "h1x", "src/b.py": "h2", "src/new.py": "h3", "check.py": "h9", "data": None}
    sig = ev.cheat_signals(traj, init, ws, {"allowed_write": ["src/"]}, {"check.py", "expected.json"}, 6 << 20)
    kinds = sorted(s["signal"] for s in sig)
    assert kinds == ["git_history", "git_history", "hidden_name_collision", "output_size", "verify_mention", "write_outside_allowed"]
    assert next(s for s in sig if s["signal"] == "write_outside_allowed")["detail"] == "a.py,check.py"
    assert ev.cheat_signals(None, init, {"a.py": "h1", "src/b.py": "zz"}, {"allowed_write": ["src/"]}, set(), 10) == []
    assert ev.cheat_signals(None, init, {}, {}, set(), 10) == []                      # no allowed_write -> no write check
    assert [s["signal"] for s in ev.cheat_signals(None, init, {"src/b.py": "h2"}, {"allowed_write": ["src/"]}, set(), 1)] == ["write_outside_allowed"]  # deletion of a.py


if __name__ == "__main__":  # tiny runner when pytest is not installed
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try: fn(); print("PASS", fn.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1; print("FAIL", fn.__name__, repr(e)); traceback.print_exc()
    print(f"{len(fns) - failed}/{len(fns)} passed"); sys.exit(1 if failed else 0)
