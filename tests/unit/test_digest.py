"""eval/digest.py: the shape of a whole run, read without a model, for the meta agent."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import digest as D  # noqa: E402


def traj(cmds, status="Submitted", submission=""):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for c in cmds: msgs += [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "bash", "arguments": json.dumps({"command": c})}}]}, {"role": "tool", "content": "ok"}]
    return {"messages": msgs, "info": {"exit_status": status, "submission": submission}}


def test_a_run_that_only_reads_until_it_hits_the_limit_is_called_that():
    d = D.digest(traj(["pwd && ls -la", "sed -n '1,80p' a.py", "grep -n x b.py", "cat c.py"] * 5, status="LimitsExceeded"))
    assert "never wrote a file" in d and "never ran a check" in d and "stopped by LimitsExceeded with nothing changed: it only read" in d and "20 commands" in d


def test_when_the_first_write_and_the_first_check_came_is_counted_and_repeats_are_noticed():
    d = D.digest(traj(["ls", "cat a.py", "cat > b.py <<'EOF'\nx=1\nEOF", "python3 -m pytest -q", "python3 -m pytest -q", "git status"], submission="done"))
    assert "first write at step 3" in d and "first check at step 4" in d and "1 identical commands in a row" in d and "git 1" in d and "last command: git status" in d


def test_a_question_is_recorded_and_an_empty_trajectory_does_not_crash():
    assert "asked a question" in D.digest(traj(["ls"], submission="QUESTION: how many?"))
    assert D.digest({}) == "no commands in the trajectory" and D.digest(None) == "no commands in the trajectory"
