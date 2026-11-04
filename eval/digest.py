"""A one-paragraph reading of a run's trajectory, with no model in it (the part of AHE's Agent Debugger that does not need one).
The meta agent is shown the last steps of each failed run; what it cannot see from there is the shape of the whole run: how long it read before it wrote, whether it ever ran a check,
whether it went in circles. That shape is what found the two largest faults in the first baseline on the user's own tasks (runs that read for 30 steps and never wrote a file;
runs that stopped at a cost limit that was priced too high), so it is computed, not left to be noticed."""
import json, re

WRITE_RE = re.compile(r"cat\s*>|<<\s*'?EOF|sed\s+-i|apply_patch|python3?\s+-\s*<<|\btee\b|\bpatch\b|git\s+apply|>>\s*\S|\bmv\b|\bcp\b")
TEST_RE = re.compile(r"pytest|unittest|\bnpm\s+(?:run\s+)?test|\bcargo\s+test|\bgo\s+test|\bmake\s+test|python3?\s+\S*test[_-]\w+\.py")
READ_RE = re.compile(r"^\s*(?:sed\s+-n|cat\s|head|tail|grep|rg\b|ls\b|find\b|wc\b|nl\s|tree|pwd|file\s|stat\s)")
GIT_RE = re.compile(r"\bgit\s")


def commands(traj):
    """The shell commands the agent issued, in order, from a mini-swe-agent style trajectory."""
    out = []
    for m in (traj or {}).get("messages", []):
        if m.get("role") != "assistant": continue
        got = False
        for tc in m.get("tool_calls") or []:
            try: out.append(json.loads(tc["function"]["arguments"]).get("command", "")); got = True
            except (ValueError, KeyError, TypeError): pass
        if not got:
            for a in (m.get("extra") or {}).get("actions") or []:
                if isinstance(a, dict) and a.get("command"): out.append(a["command"])
    return out


def digest(traj):
    cmds = commands(traj); info = (traj or {}).get("info") or {}
    if not cmds: return "no commands in the trajectory"
    is_write = [bool(WRITE_RE.search(c)) for c in cmds]
    first_write = next((i + 1 for i, w in enumerate(is_write) if w), None)
    first_test = next((i + 1 for i, c in enumerate(cmds) if TEST_RE.search(c) and not WRITE_RE.search(c)), None)
    counts = {"read": sum(1 for c, w in zip(cmds, is_write) if READ_RE.match(c) and not w), "write": sum(is_write), "test": sum(1 for c in cmds if TEST_RE.search(c)), "git": sum(1 for c in cmds if GIT_RE.search(c))}
    repeats = sum(1 for a, b in zip(cmds, cmds[1:]) if a.strip() == b.strip())
    parts = [f"{len(cmds)} commands (read {counts['read']}, write {counts['write']}, test {counts['test']}, git {counts['git']})",
             f"first write at step {first_write}" if first_write else "never wrote a file",
             f"first check at step {first_test}" if first_test else "never ran a check"]
    if repeats: parts.append(f"{repeats} identical commands in a row")
    status = info.get("exit_status")
    if status in ("LimitsExceeded", "BudgetExceeded", "TimeExceeded"):
        parts.append(f"stopped by {status}" + (" with nothing changed: it only read" if not first_write else ""))
    sub = str(info.get("submission") or "")
    if sub.strip().upper().startswith("QUESTION:"): parts.append("asked a question")
    parts.append("last command: " + cmds[-1].strip().splitlines()[0][:100])
    return "; ".join(parts)
