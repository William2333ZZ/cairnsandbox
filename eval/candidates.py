#!/usr/bin/env python3
"""Candidates written by a person, ready for `evolve.py candidate` (docs/007: the road a person's change takes is the meta agent's road).
A candidate is the whole harness directory plus CHANGES.md and MANIFEST.json (what changed, why, which tasks it should fix). Each named one here is a small, explicit change to the
repository's own harness, with the evidence that suggested it.

usage: python3 candidates.py <name> --out <dir> [--aios <path to the aios/ directory>] [--fixes task,task]
       then: evolve.py candidate --taskset T --k 2 --parent <version> --dir <dir> --changes <dir>/CHANGES.md --manifest <dir>/MANIFEST.json --base-batch <baseline batch>"""
import argparse, json, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def nudge(cfg, fixes):
    cfg["agent"]["middleware"] = list(cfg["agent"].get("middleware") or []) + [{"name": "progress_nudge", "at": 15}]
    return ("- At model call 15 of 60, if no file in the working tree has changed since the start, the agent is told once: stop exploring, make a first small change, run the checks; say so now if a decision only the person can make is missing.\n"
            "- Why: in the first baseline on the user's own tasks two of eleven runs read for 30 steps and never wrote a file, and five first wrote at step 16 or later (docs/history/d17 §七).\n"
            "- Nothing else changes: the middleware list is empty in the seed; this candidate adds one entry to it in config.json.",
            [{"component": "middleware", "files": ["aios/config.json"], "why": "runs that only read hit the limit with nothing to show; a single reminder at call 15 asks for a first change",
              "evidence": fixes, "predicts": {"fixes": fixes, "regresses": []}, "constraint": "one message, once, only when nothing has changed"}])


def asking(cfg, fixes):
    t = cfg["agent"]["instance_template"]
    if "## Recommended Workflow" not in t: raise SystemExit("the prompt has no '## Recommended Workflow' heading to put the paragraph before")
    add = ("\n\n## Missing decisions\n\nIf the request leaves out something that changes what you would build (a number, a name, a choice between two behaviours) and the repository does not settle it, "
           "ask the person once before you build, with the QUESTION: form described in the task. A question costs them one sentence; a wrong guess costs a review round. Do not ask about anything you can find by reading.\n")
    cfg["agent"]["instance_template"] = t.replace("## Recommended Workflow", add + "\n## Recommended Workflow", 1)
    return ("- The workflow section of the prompt gets a short paragraph that says when asking is part of the job.\n"
            "- Why: on the missing-decision tasks the agent asked in 0 of 7 runs; in one it wrote that asking a human was 'not appropriate'.",
            [{"component": "system_prompt", "files": ["aios/config.json"], "why": "the agent does not ask even when it notices the gap; say that asking is expected here",
              "evidence": fixes, "predicts": {"fixes": fixes, "regresses": []}, "constraint": "ask at most once; never about what the repository answers"}])


CANDIDATES = {"nudge": nudge, "ask-paragraph": asking}


def build(name, out, aios, fixes):
    if name not in CANDIDATES: raise SystemExit(f"unknown candidate {name!r}; known: {', '.join(CANDIDATES)}")
    real = lambda p: os.path.realpath(p) + os.sep                                       # noqa: E731
    if real(aios).startswith(real(out)) or real(os.getcwd()).startswith(real(out)): raise SystemExit("--out must not contain the harness or the working directory: it is emptied first")
    shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
    shutil.copytree(aios, os.path.join(out, "aios"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    p = os.path.join(out, "aios", "config.json"); cfg = json.load(open(p))
    changes, manifest = CANDIDATES[name](cfg, fixes)
    json.dump(cfg, open(p, "w"), indent=1, ensure_ascii=False)
    open(os.path.join(out, "CHANGES.md"), "w").write(changes + "\n")
    open(os.path.join(out, "MANIFEST.json"), "w").write(json.dumps({"changes": manifest}, ensure_ascii=False, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("name"); ap.add_argument("--out", required=True); ap.add_argument("--aios", default=os.path.join(HERE, "..", "aios")); ap.add_argument("--fixes", default="")
    a = ap.parse_args(); print(build(a.name, a.out, a.aios, [x for x in a.fixes.split(",") if x]))
