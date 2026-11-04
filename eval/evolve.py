#!/usr/bin/env python3
"""fc-evolve: the automated version loop (docs/007-evaluation.md §级联/§接受, docs/004-guest.md §晋升路径).

One iteration:
  1. parent      the current best archive node (seed = the evaluator's default version), with its baseline batch (run if missing)
  2. propose     a meta agent in a dev sandbox reads the parent's harness source (/aios/harness/aios, mounted read-only) and the
                 parent's failed trajectories, writes an edited copy under /work/harness and a CHANGES.md
  3. critic      diff-level: reject edits that name tasks, answers or verify paths; classify tampering of eval/record/propagate code
  4. build       a build sandbox on the langs layer installs the candidate harness + launchers, selfchecks, packs -> candidate layer
  5. register    evaluator version {layers: [langs, candidate, env...], parent}
  6. compare     evaluator compare batch (cascade: smoke -> core) against the parent's baseline; accept per the evaluator's rule
  7. archive     node {version, parent, batch, accept, stats, changes, manifest, attribution}; an accepted candidate becomes the new parent
Every candidate carries a change manifest (what was changed, why, which tasks it expects to fix and which to break); after the compare the driver
sets the prediction against what happened per task (attribution) -- a self-improving harness is blind to what its own edits break (AHE 2026),
so the numbers, not the story, decide, and the gap between them is kept.
Everything measured comes from the evaluator; this driver never scores anything itself. State: <EVAL_STATE>/evolve.json.
usage (inside the fc-eval container or with EVAL_URL/CP_URL/MODEL_PROXY_URL set):
  evolve.py run --taskset polyglot-py --iterations 2 --k 3 [--seed-version <id>] [--budget-usd 10]
  evolve.py status
"""
import argparse, io, json, os, re, sys, tarfile, time, urllib.error, urllib.parse, urllib.request, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from digest import digest      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = r'''#!/bin/sh
export PYTHONPATH=/aios/harness${PYTHONPATH:+:$PYTHONPATH} PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1
sub=${1:-run}; shift || true
exec /aios/runtime/python/bin/python3 -P -m "aios.$sub" "$@"
'''
PROPOSE_PROMPT = """You are improving an AI coding agent's harness (its prompts, configuration and loop code). The harness that is
currently running YOU is the parent version; its source is mounted read-only at /aios/harness/aios. A writable copy is at
/work/harness/aios. Your job: make ONE small, general improvement to the copy that would help the agent solve more tasks
like the failed ones below, without overfitting to any specific task.

This round's direction: {direction}. Stay on it unless the failures clearly point elsewhere.

Rules:
- Edit only files under /work/harness/aios. The parts you may change, each a file you can diff on its own: system_prompt (config.json system_template, instance_template), tool_description (BASH_TOOL in model.py),
  tool_impl (env.py, the observation template in model.py), middleware (a class in aios/middleware.py switched on by name in config.json "agent"."middleware"; hooks before_step, before_query, after_step),
  skill (a script or SKILL.md under aios/skills/ that the prompt tells the agent to use), subagent (not available yet), memory (aios/memory.md, notes the agent starts every task with).
  Prefer a new middleware entry or a memory note over a prompt rewrite: in the published ablation a prompt-only change did not help while middleware, tools and memory did. Hooks that rewrite the early conversation break the provider's prefix cache and make the run dearer.
- Keep the command-line interface and exit codes of `aios.run` unchanged; keep the model client talking to CAIRN_MODEL_URL.
- Do not mention any task name, expected answer or test file. Do not add task-specific branches.
- Keep the change under ~120 changed lines. Prefer changes to the system/instance prompt, observation formatting, or
  error handling that a generic coding agent benefits from.
- A diff-level critic rejects edits that name verification material: never write the strings "/verify/", "verify.sh",
  "reference/" or "SCORE" into the harness, and never mention a specific task name.
- Verify the copy still imports: `cd /work/harness && /aios/runtime/python/bin/python3 -P -c "import aios.run"`.
- Write /work/CHANGES.md: 3-8 lines, what you changed and why (which failure pattern it addresses).
- Write /work/MANIFEST.json (outside the harness, never part of it): {{"changes": [{{"component": "system_prompt|tool_description|tool_impl|middleware|skill|subagent|memory", "files": ["aios/model.py"],
  "why": "the failure pattern, one sentence", "evidence": ["<task id from the failures above>"], "predicts": {{"fixes": ["<task ids you expect to start passing>"],
  "regresses": ["<task ids this change might break>"]}}, "constraint": "what must stay true"}}]}}. Name only task ids that appear above; an honest empty list beats a guess.

Parent version: {version}. Its scores on the evaluation set: mean {mean}, {n_fail} of {n_runs} runs failed.

Failed trajectories (truncated; last steps of each):
{failures}

When done, finish with `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` followed by one line summarising the change.
"""
DIRECTIONS = [   # the harness components (docs/004 §8 个组件), rotated so the proposals do not all land on the system prompt
    "observation formatting: how command output, errors and long outputs are shown to the model (model.py observation template, truncation, head/tail)",
    "error handling and recovery in the loop: repeated identical commands, failed commands, format errors, when to stop (agent.py)",
    "the instance prompt's workflow: what to read first, when to write a test, when to submit (config.json instance_template)",
    "tool semantics: the bash tool's description and the examples of file editing/viewing commands (config.json, model.py BASH_TOOL)",
    "a helper script or skill the agent can call from bash for a recurring sub-task (new file under aios/, invoked via the prompt)",
    "the system prompt: role, discipline, verification habits (config.json system_template)",
    "asking: when the request leaves out something only the person can say (a value, a choice, a name), stop exploring and finish with ONE question (the QUESTION: submission described in the ticket text) instead of searching the repository for it or guessing; and when to go on under a stated assumption instead (the answer is in the repository, or any reasonable choice would do)",
]
DIRECTION_BY_NAME = {"observation": 0, "loop": 1, "workflow": 2, "tools": 3, "helper": 4, "prompt": 5, "ask": 6}      # `evolve.py run --direction ask` aims one iteration at a component
TASK_LIKE_RE = re.compile(r"(verify\.sh|/verify/|\breference/|SCORE\s+[01]\b)", re.I)   # verification paths and score tokens; a prompt may still talk about tests in general
TAMPER_RE = re.compile(r"(abi\.py|budget_charge|events\.jsonl|X-Cost-USD|tokens_in|cost_usd|report\(|/run/aios)", re.I)


def cfg_from_env():
    return {"eval_url": os.environ.get("EVAL_URL", "http://127.0.0.1:8600").rstrip("/"),
            "cp_url": os.environ.get("CP_URL", "http://127.0.0.1:18700").rstrip("/"), "cp_token": os.environ.get("CP_TOKEN", ""),
            "model_proxy": os.environ.get("MODEL_PROXY_URL", "").rstrip("/"), "model_name": os.environ.get("CAIRN_MODEL_NAME", "deepseek-flash"),
            "state": os.environ.get("EVAL_STATE", os.path.join(HERE, "state")), "template": os.environ.get("EVAL_TEMPLATE", "debian-13"),
            "layers": [l for l in os.environ.get("EVAL_LAYERS", "").split(",") if l],
            "meta_budget_usd": float(os.environ.get("EVOLVE_META_BUDGET_USD", "0.5")), "meta_timeout_s": int(os.environ.get("EVOLVE_META_TIMEOUT_S", "900")),
            "intake_url": os.environ.get("INTAKE_URL", "").rstrip("/"),     # when set, each finished iteration files an evaluation Trigger (docs/005 自动 Source)
            "home": os.environ.get("EVOLVE_HOME", "")}                        # when set, an accepted candidate is also written as a proposal under this harness home (D29)


def now(): return time.time()


class Http:
    def __init__(self, base, token=""): self.base, self.token = base.rstrip("/"), token

    def __call__(self, method, path, body=None, raw=False, timeout=600):
        data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/octet-stream" if isinstance(body, (bytes, bytearray)) else "application/json")
        if self.token: req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                p = r.read(); return p if raw else (json.loads(p) if p else {})
        except urllib.error.HTTPError as e: raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:300]!r}") from None


def tar_bytes(files):
    """{relative path: bytes} -> tar bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in sorted(files.items()):
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644; ti.mtime = int(now()); tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def untar(data):
    """{path: bytes} for the regular files of a tar, without bytecode caches (a layer may carry stale ones)."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf:
            name = os.path.normpath(m.name)
            if m.isfile() and "__pycache__" not in name.split(os.sep) and not name.endswith(".pyc"): out[name] = tf.extractfile(m).read()
    return out


def unified_diff(old, new):
    import difflib
    lines = []
    for name in sorted(set(old) | set(new)):
        a = old.get(name, b"").decode("utf-8", "replace").splitlines(keepends=True); b = new.get(name, b"").decode("utf-8", "replace").splitlines(keepends=True)
        if a != b: lines += list(difflib.unified_diff(a, b, fromfile="a/" + name, tofile="b/" + name))
    return "".join(lines)


def critic(diff, task_ids):
    """Diff-level critic (docs/007): reject task-specific edits; flag tampering with measurement paths. Returns (ok, reasons)."""
    reasons = []
    added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    for tid in task_ids:                                        # the full task id, or a distinctive exercise name as a whole word
        name = tid.split("-", 1)[1] if "-" in tid else tid
        if re.search(r"\b" + re.escape(tid) + r"\b", added) or (len(name) >= 8 and re.search(r"\b" + re.escape(name).replace(r"\-", "[-_ ]") + r"\b", added, re.I)):
            reasons.append(f"names task {tid}")
    if TASK_LIKE_RE.search(added): reasons.append("refers to verification paths or score tokens")
    if TAMPER_RE.search(added): reasons.append("touches measurement or reporting paths (tamper class)")
    removed = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    if TAMPER_RE.search(removed): reasons.append("removes measurement or reporting calls (tamper class)")      # taking the budget charge or the report out of the harness is as much a tamper as writing to it
    changed = sum(1 for l in diff.splitlines() if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---")))
    if changed > 400: reasons.append(f"diff too large ({changed} lines)")
    return (not reasons), reasons, changed


def update_payload(vid, version, node, base, taskset, cand_cost, base_cost, st, extra=None):
    """What the Agent service needs to switch versions and to say one honest sentence: the layers, the change, why, the numbers -- and what was expected against what happened."""
    ci = (st.get("paired") or {}).get("ci") or st.get("ci")
    p = {"version": vid, "label": version.get("label") or vid, "layers": version.get("layers") or [], "parent": node.get("parent"), "changes": (node.get("changes") or node.get("note") or "")[:800],
         "taskset": taskset, "stats": {"mean": st.get("mean"), "parent_mean": base.get("mean"), "delta": base.get("delta"), "ci": ci, "cost": cand_cost, "parent_cost": base_cost}}
    m, a = node.get("manifest") or {}, node.get("attribution") or {}
    why = "; ".join(c["why"] for c in m.get("changes", []) if c.get("why"))
    if why: p["why"] = why[:400]
    if a: p["attribution"] = {k: (len(a[k]) if isinstance(a.get(k), list) else a.get(k)) for k in ("fixed", "broke", "lost_stable", "predicted_fixes", "hit_fixes", "unpredicted_broke", "solved_by_parent", "n_tasks")}
    for k, v in (extra or {}).items():
        if v: p[k] = v
    return p


COMPONENTS = ("system_prompt", "tool_description", "tool_impl", "middleware", "skill", "subagent", "memory", "config")      # the seven editable parts of AHE (arXiv 2604.25850) plus a catch-all
COMPONENT_ALIASES = {"observation": "tool_impl", "loop": "middleware", "prompt": "system_prompt", "tools": "tool_description", "helper": "skill"}   # the names earlier manifests and DIRECTIONS used
SCORING = "2"                  # the evaluator's SCORING_VERSION (a test keeps the two equal): a baseline scored another way is not reused and not compared
SHIFT = 0.25                    # a task counts as fixed / broken when its mean score moves by at least this much between parent and candidate


def parse_manifest(text, task_ids, files):
    """The meta agent's MANIFEST.json -> {changes: [...], dropped: [reasons]}. Ids it cannot know (not among the tasks it saw) and files the diff did not touch are
    dropped, not trusted; a manifest that does not parse is {changes: [], error}."""
    try: raw = json.loads(text)
    except (ValueError, TypeError): return {"changes": [], "error": "MANIFEST.json missing or not JSON"}
    known, changes, dropped = set(task_ids), [], []
    lst = lambda x: [v for v in x if isinstance(v, str)] if isinstance(x, list) else []        # a model may write a number or a string where a list belongs
    for c in (raw.get("changes") if isinstance(raw, dict) and isinstance(raw.get("changes"), list) else None) or []:
        if not isinstance(c, dict): continue
        comp = COMPONENT_ALIASES.get(c.get("component"), c.get("component")); comp = comp if comp in COMPONENTS else "config"
        pred = c.get("predicts") if isinstance(c.get("predicts"), dict) else {}
        keep = lambda xs: [x for x in lst(xs) if x in known]  # noqa: E731
        for x in lst(pred.get("fixes")) + lst(pred.get("regresses")) + lst(c.get("evidence")):
            if x not in known: dropped.append(f"unknown task id {x[:40]}")
        changes.append({"component": comp, "files": [f for f in lst(c.get("files")) if f in files][:8], "why": str(c.get("why") or "")[:300],
                        "evidence": keep(c.get("evidence")), "predicts": {"fixes": keep(pred.get("fixes")), "regresses": keep(pred.get("regresses"))}, "constraint": str(c.get("constraint") or "")[:200]})
    return {"changes": changes[:6], "dropped": dropped[:10]}


def attribute(manifest, cand_pt, base_pt, shift=SHIFT):
    """Prediction against outcome, per task. cand_pt / base_pt: {task: {p_hat, pass_k}} from the two batches' stats. `lost_stable` are tasks the parent passed on
    every run and the candidate no longer does: the regression the meta agent could not see, and the reason a mean that went up is not yet an improvement."""
    common = sorted(set(cand_pt) & set(base_pt))
    d = {t: cand_pt[t]["p_hat"] - base_pt[t]["p_hat"] for t in common}
    fixed = [t for t in common if d[t] >= shift]; broke = [t for t in common if d[t] <= -shift]
    lost = [t for t in common if base_pt[t].get("pass_k") and not cand_pt[t].get("pass_k")]
    pf = sorted({t for c in (manifest or {}).get("changes", []) for t in c["predicts"]["fixes"]}); pr = sorted({t for c in (manifest or {}).get("changes", []) for t in c["predicts"]["regresses"]})
    hit = sorted(set(pf) & set(fixed)); rhit = sorted(set(pr) & set(broke))
    return {"n_tasks": len(common), "fixed": fixed, "broke": broke, "lost_stable": lost, "predicted_fixes": pf, "hit_fixes": hit,
            "fix_precision": (len(hit) / len(pf)) if pf else None, "fix_recall": (len(hit) / len(fixed)) if fixed else None,
            "predicted_regresses": pr, "hit_regresses": rhit, "unpredicted_broke": sorted(set(broke) - set(pr)),
            "solved_by_parent": sum(1 for t in common if base_pt[t].get("pass_k"))}


PARETO_COST = 0.75      # the efficiency arm: a candidate that is not worse and costs at most this share of the parent's cost per run is an improvement


def pareto_ok(st, cand_cost, base_cost):
    """007 接受: the rule asks that more money buy more score; the other half is that the same score for much less money is better. Accepts a candidate whose paired score difference is not
    negative (point estimate) and whose cost per run is at most 75% of the parent's -- 'it does the same for less' is how a harness gets cheaper without anyone noticing it got worse."""
    p = st.get("paired") or {}; d = p.get("diff"); lo = (p.get("ci95") or [None, None])[0]
    return d is not None and d >= 0 and (lo is None or lo > -0.15) and base_cost > 0.01 and cand_cost <= PARETO_COST * base_cost      # a point estimate is not enough: the interval may not reach far below zero (or a candidate that merely explores less wins by noise, and ratchets)


def explain(it):
    """One paragraph a person can read: the change, why, what was predicted, what happened."""
    m, a = it.get("manifest") or {}, it.get("attribution") or {}
    why = "; ".join(c["why"] for c in m.get("changes", []) if c.get("why")) or (it.get("changes") or "")[:200]
    out = [f"改了:{(it.get('changes') or '').strip()[:200]}", f"原因:{why or '(没有写)'}"]
    if a:
        out.append(f"预计修好 {len(a['predicted_fixes'])} 道,实际修好 {len(a['fixed'])} 道(预计中了 {len(a['hit_fixes'])} 道);变差 {len(a['broke'])} 道,其中没预计到的 {len(a['unpredicted_broke'])} 道;"
                   f"原本稳定做对、现在不稳的 {len(a['lost_stable'])} 道")
    return "。".join(out)



# ---- D29: an accepted candidate becomes a proposal under the person's harness home; only `aios.home apply` moves it in
HOME_MAP = {"aios/config.json": "config.json", "aios/memory.md": "memory.md"}          # harness file -> home file; directories map by name
HOME_DIRS = ("prompts", "skills", "middleware", "rules")


def config_overlay(parent, cand):
    """The smallest overlay that, merged over `parent` the way aios.home.merge does (dict into dict, anything else replaces), gives `cand`.
    A key the candidate dropped cannot be expressed by an overlay and is reported under "_removed"."""
    out = {}
    for k, v in cand.items():
        pv = parent.get(k)
        if isinstance(v, dict) and isinstance(pv, dict):
            sub = config_overlay(pv, v)
            if sub: out[k] = sub
        elif v != pv: out[k] = v
    removed = [k for k in parent if k not in cand]
    if removed: out["_removed"] = removed
    return out


def home_proposal(pfiles, cand_files):
    """Map a candidate harness (files under aios/) onto the home layout (CONTRACT §5a). Returns (files, deletes, code):
    files {home path: bytes} to write, deletes [home paths] the candidate removed, code [harness paths] changed outside what a home can hold (they travel with the layer only)."""
    files, deletes, code = {}, [], []
    for name in sorted(set(pfiles) | set(cand_files)):
        if pfiles.get(name) == cand_files.get(name): continue
        rel = name[5:] if name.startswith("aios/") else None
        target = HOME_MAP.get(name) or (rel if rel and rel.split("/")[0] in HOME_DIRS and "/" in rel else None)
        if name.endswith(".pyc") or "__pycache__" in name: continue
        if not target: code.append(name); continue
        if name not in cand_files: deletes.append(target); continue
        if name == "aios/config.json":
            try: over = config_overlay(json.loads(pfiles.get(name) or b"{}"), json.loads(cand_files[name]))
            except ValueError: code.append(name); continue
            if over.pop("_removed", None): code.append(name + " (keys removed; an overlay cannot drop a key)")
            if not over: continue
            files[target] = (json.dumps(over, ensure_ascii=False, indent=2) + "\n").encode()
        else: files[target] = cand_files[name]
    return files, deletes, code


def write_proposal(home, pid, files, deletes, text):
    """Write one proposal directory under <home>/proposals/<pid>: the files in home layout, a DELETE list, and PROPOSAL.md explaining the change (aios.home apply skips the latter two)."""
    d = os.path.join(home, "proposals", pid)
    if os.path.isdir(d): raise FileExistsError(f"proposal {pid} already exists under {home}")
    for rel, data in files.items():
        q = os.path.join(d, rel); os.makedirs(os.path.dirname(q), exist_ok=True)
        with open(q, "wb") as f: f.write(data)
    os.makedirs(d, exist_ok=True)
    if deletes:
        with open(os.path.join(d, "DELETE"), "w") as f: f.write("\n".join(deletes) + "\n")
    with open(os.path.join(d, "PROPOSAL.md"), "w", encoding="utf-8") as f: f.write(text)
    return d


def proposal_text(vid, it, node, files, deletes, code):
    cmp_ = node.get("compare") or {}; base = (node.get("baseline") or {})
    lines = [f"# 提案 {vid}", "", f"- 来源:evolve 迭代 `{it.get('id')}`,父版本 `{it.get('parent')}`,题集 `{it.get('taskset')}`(k={it.get('k')})",
             f"- 评测:{it.get('result') or cmp_.get('reason') or ''}", f"- 均分:{cmp_.get('mean')},每次运行成本 {cmp_.get('cost_per_run')} vs 父 {cmp_.get('parent_cost_per_run')}",
             f"- 层:`{it.get('layer')}`;对比批次 `{cmp_.get('batch') or base.get('batch')}`", "", "## 改了什么", "", (it.get("changes") or "").strip() or "(没有说明)", "",
             "## 目录里的文件", ""] + ([f"- `{r}`" for r in sorted(files)] or ["- (无)"])
    if deletes: lines += ["", "## 删除", ""] + [f"- `{r}`" for r in deletes]
    if code: lines += ["", "## 不在目录范围的改动(只随层走,apply 不会搬)", ""] + [f"- `{r}`" for r in code]
    lines += ["", "接受它:`python -m aios.home apply " + vid + "`(目录 check 不过会原样放回)。", ""]
    return "\n".join(lines)


class Evolve:
    def __init__(self, cfg):
        self.cfg = cfg; self.ev = Http(cfg["eval_url"]); self.cp = Http(cfg["cp_url"], cfg["cp_token"])
        self.proxy = Http(cfg["model_proxy"]) if cfg["model_proxy"] else None
        os.makedirs(cfg["state"], exist_ok=True); self.path = os.path.join(cfg["state"], "evolve.json")
        self.state = json.load(open(self.path)) if os.path.exists(self.path) else {"nodes": [], "iterations": [], "spent_usd": 0.0}
        for it in self.state["iterations"]:                       # a run that died (killed process, host restart) left no result
            if it.get("result") is None and not it.get("ended"): it["result"] = "aborted: the driver did not finish"; it["ended"] = now()
        seeds = sorted([n for n in self.state["nodes"] if n.get("note") == "seed"], key=lambda n: n["created"])
        for o in seeds[:-1]: o["superseded"] = True                # only the newest seed is a root (older harness builds are history)

    def save(self): json.dump(self.state, open(self.path, "w"), indent=1)

    # ---- evaluator helpers
    def wait_batch(self, bid, poll=5):
        while True:
            b = self.ev("GET", f"/batches/{bid}")
            if b.get("status") in ("done", "failed"): return b
            time.sleep(poll)

    def baseline(self, version_id, taskset, k, tasks=None):
        tsv = (self.ev("GET", f"/tasksets/{taskset}") or {}).get("version")
        for n in self.state["nodes"]:
            b0 = n.get("baseline") or {}
            if n["version"] == version_id and b0.get("taskset") == taskset and b0.get("k") == k and b0.get("status") == "done" and b0.get("scoring") == SCORING and b0.get("taskset_version") == tsv: return b0       # a baseline from before the scoring or the task set changed is a different measurement
        body = {"kind": "baseline", "taskset": taskset, "version": version_id, "k": k, "label": f"evolve baseline {version_id}"}
        if tasks: body["tasks"] = tasks
        bid = self.ev("POST", "/batches", body)["batch_id"]; print(f"baseline batch {bid} for {version_id} ...", flush=True)
        b = self.wait_batch(bid); st = b.get("stats") or {}
        if b.get("status") != "done": raise RuntimeError(f"baseline batch {bid} ended {b.get('status')}: {(b.get('summary') or {}).get('error')}")
        base = {"batch": bid, "taskset": taskset, "k": k, "scoring": (b.get("params") or {}).get("scoring"), "taskset_version": (b.get("params") or {}).get("taskset_version"), "status": b["status"], "mean": st.get("mean"), "delta": st.get("delta"), "progress": b.get("progress")}
        self.state["spent_usd"] += sum(x.get("cost") or 0 for rs in (b.get("matrix") or {}).values() for x in rs)
        node = self.node(version_id) or self.add_node(version_id, None, note="seed")
        node["baseline"] = base; self.save(); return base

    def node(self, vid): return next((n for n in self.state["nodes"] if n["version"] == vid), None)

    def add_node(self, vid, parent, note="", **kw):
        n = {"version": vid, "parent": parent, "note": note, "created": now(), "accepted": None, **kw}
        if note == "seed":                                            # a new seed supersedes the older seeds (their harness build is history)
            for o in self.state["nodes"]:
                if o.get("note") == "seed" and o["version"] != vid: o["superseded"] = True
        self.state["nodes"].append(n); self.save(); return n

    def eligible(self):
        """Parents: the current seed and every accepted descendant of it; superseded seeds and their clades are history."""
        roots = [n for n in self.state["nodes"] if n.get("note") == "seed" and not n.get("superseded")]
        if not roots: return []
        by_parent = {}
        for n in self.state["nodes"]: by_parent.setdefault(n.get("parent"), []).append(n)
        out, stack = [], list(roots)
        while stack:
            n = stack.pop()
            if n.get("note") == "seed" or n.get("accepted"): out.append(n)
            stack += by_parent.get(n["version"], [])
        return out

    def best(self):
        acc = list(self.eligible())
        acc.sort(key=lambda n: ((n.get("baseline") or {}).get("mean") or 0, n["created"])); return acc[-1] if acc else None

    def choose_parent(self, rng=None):
        """Parent selection (docs/007, after HGM): eligible nodes are the seed and accepted candidates. Each gets a Beta posterior
        over its clade's success (1 + accepted descendants, 1 + rejected descendants); draw a Thompson sample, divide by
        1 + direct children so well-explored nodes yield to fresh ones, and take the arg max. With a single eligible node
        this is `best()`; the node's own score never enters the draw (it predicts offspring poorly)."""
        import random
        rng = rng or random.Random()
        nodes = self.state["nodes"]; eligible = self.eligible()
        if not eligible: return None
        if len(eligible) == 1: return eligible[0]
        children = {}
        for n in nodes: children.setdefault(n.get("parent"), []).append(n)
        def clade(vid):
            acc = rej = 0; stack = list(children.get(vid, []))
            while stack:
                c = stack.pop(); acc += 1 if c.get("accepted") else 0; rej += 1 if c.get("accepted") is False else 0; stack += children.get(c["version"], [])
            return acc, rej
        scored = []
        for n in eligible:
            acc, rej = clade(n["version"]); draw = rng.betavariate(1 + acc, 1 + rej) / (1 + len(children.get(n["version"], [])))
            scored.append((draw, n))
        return max(scored, key=lambda x: x[0])[1]

    def failures(self, batch_id, limit=6):
        b = self.ev("GET", f"/batches/{batch_id}"); out = []
        for tid, rs in (b.get("matrix") or {}).items():
            for r in rs:
                if r.get("pass") or r.get("infra_fail") or len(out) >= limit: continue
                run = self.ev("GET", f"/runs/{r['run_id']}"); traj = run.get("traj") or {}
                msgs = [m for m in traj.get("messages", []) if m.get("role") in ("assistant", "user")][-6:]
                tail = "\n".join(f"[{m.get('role')}] {str(m.get('content', ''))[:600]}" for m in msgs)
                out.append(f"--- task {tid}: score {r.get('score')}, exit_status {traj.get('info', {}).get('exit_status')}, {r.get('tokens_in')} tokens in\nshape of the whole run: {digest(traj)}\nlast steps:\n{tail}\n")
        return out

    # ---- sandboxes
    def _spec(self, layers, ttl, hosts):
        return {"class": "ephemeral", "template": self.cfg["template"], "layers": layers, "vcpu": 1, "mem": 512, "disk": 1024, "ttl_max_s": ttl + 300, "network": {"allow_hosts": hosts}}

    def _exec(self, sid, cmd, user="root", timeout=120, env=None, cwd=None, landlock=None):
        body = {"cmd": cmd, "user": user, "timeout": timeout}
        if env: body["env"] = env
        if cwd: body["cwd"] = cwd
        if landlock is not None: body["landlock"] = landlock
        return self.cp("POST", f"/sandboxes/{sid}/exec", body, timeout=timeout + 60)

    def propose(self, parent, version, taskset_tasks):
        """Meta agent step. Returns (candidate files {path: bytes}, parent files, CHANGES.md text, usage)."""
        layers = version["layers"] or self.cfg["layers"]
        hosts = [urllib.parse.urlparse(self.cfg["model_proxy"]).hostname] if self.cfg["model_proxy"] else []
        sid = self.cp("POST", "/sandboxes", self._spec(layers, self.cfg["meta_timeout_s"], hosts))["id"]
        try:
            self._exec(sid, "mkdir -p /work/harness && cp -R /aios/harness/aios /work/harness/aios && rm -rf /work/harness/aios/__pycache__ && chown -R agent:agent /work")
            parent_files = untar(self.cp("GET", f"/sandboxes/{sid}/files?path=/tmp/parent.tar", raw=True)) if self._exec(sid, "tar -C /aios/harness -cf /tmp/parent.tar aios")["exit"] == 0 else {}
            base = parent.get("baseline") or {}; fails = self.failures(base["batch"]) if base.get("batch") else []
            prog = base.get("progress") or {}
            n_runs = prog.get("done") or 0
            direction = parent.get("_direction") or DIRECTIONS[0]
            prompt = PROPOSE_PROMPT.format(version=version["id"], mean=base.get("mean"), n_fail=len(fails), n_runs=n_runs, direction=direction, failures="\n".join(fails) or "(no failed trajectories available)")
            self.cp("PUT", f"/sandboxes/{sid}/files?path=/work/.meta-task.md", prompt.encode())
            env = {"CAIRN_MODEL_NAME": self.cfg["model_name"]}
            if self.proxy:
                tok = self.proxy("POST", "/tokens", {"sandbox": sid, "budget_usd": self.cfg["meta_budget_usd"], "account": "eval"}).get("token", "")
                env["CAIRN_MODEL_URL"], env["CAIRN_MODEL_KEY"] = self.cfg["model_proxy"] + "/v1", tok
            cmd = "/aios/bin/aios run --task-file /work/.meta-task.md --config /aios/harness/aios/config.json --out /work/.meta-traj.json --cwd /work"
            r = self._exec(sid, cmd, user="agent", timeout=self.cfg["meta_timeout_s"], env=env, cwd="/work", landlock=True)
            self._exec(sid, "rm -rf /work/harness/aios/__pycache__; tar -C /work/harness -cf /tmp/cand.tar aios; cp /work/CHANGES.md /tmp/CHANGES.md 2>/dev/null || true; cp /work/MANIFEST.json /tmp/MANIFEST.json 2>/dev/null || true")
            cand = untar(self.cp("GET", f"/sandboxes/{sid}/files?path=/tmp/cand.tar", raw=True))
            try: changes = self.cp("GET", f"/sandboxes/{sid}/files?path=/tmp/CHANGES.md", raw=True).decode("utf-8", "replace")
            except RuntimeError: changes = ""
            try: manifest_text = self.cp("GET", f"/sandboxes/{sid}/files?path=/tmp/MANIFEST.json", raw=True).decode("utf-8", "replace")
            except RuntimeError: manifest_text = ""
            self._manifest_text = manifest_text
            usage = self.proxy("GET", f"/usage/{sid}") if self.proxy else {}
            return cand, parent_files, changes, {"exit": r.get("exit"), "cost": float(usage.get("cost_usd") or 0), "tokens_in": usage.get("tokens_in"), "tokens_out": usage.get("tokens_out"), "log_tail": ((r.get("stdout") or "") + "\n--- stderr ---\n" + (r.get("stderr") or ""))[-900:]}
        finally:
            self.cp("DELETE", f"/sandboxes/{sid}")

    def parent_files(self, version):
        """The harness source a version carries, read out of a sandbox that has its layers."""
        sid = self.cp("POST", "/sandboxes", self._spec(version["layers"] or self.cfg["layers"], 300, []))["id"]
        try:
            if self._exec(sid, "tar -C /aios/harness -cf /tmp/parent.tar aios")["exit"] != 0: return {}
            return untar(self.cp("GET", f"/sandboxes/{sid}/files?path=/tmp/parent.tar", raw=True))
        finally:
            self.cp("DELETE", f"/sandboxes/{sid}")

    def submit_candidate(self, taskset, k, parent_vid, cand_files, changes, manifest_text, budget_usd, base_batch=None):
        """A candidate written by a person (or by the platform's developer) goes through exactly the road a meta agent's goes: the critic reads its diff, a layer is built, the evaluator
        compares it with the parent's baseline, the verdict is the same rule, the manifest is set against what happened, an accepted one reaches the agent as an update."""
        ts = self.ev("GET", f"/tasksets/{taskset}"); task_ids = [t["id"] for t in ts["tasks"] if t.get("status") == "ok"]
        core = [t["id"] for t in ts["tasks"] if t.get("status") == "ok" and t.get("group") == "evolve"]
        parent = self.node(parent_vid) or self.add_node(parent_vid, None, note="candidate parent")           # not a seed: a seed supersedes the older seeds' whole clades
        version = next(v for v in self.ev("GET", "/versions")["versions"] if v["id"] == parent_vid)
        if base_batch:                                                      # a baseline the person already ran: its tasks are the comparison's tasks, its k is the k
            bb = self.ev("GET", f"/batches/{base_batch}"); st0 = bb.get("stats") or {}; core = sorted((bb.get("matrix") or {}).keys()); k = bb["k"]
            base = {"batch": base_batch, "taskset": taskset, "k": k, "status": bb["status"], "mean": st0.get("mean"), "delta": st0.get("delta"), "progress": bb.get("progress")}
            parent["baseline"] = base; self.save()
        else:
            if not core: raise RuntimeError(f"taskset {taskset} has no checked evolve tasks")
            base = self.baseline(parent_vid, taskset, k, tasks=core)
        it = {"id": f"it-{uuid.uuid4().hex[:8]}", "parent": parent_vid, "started": now(), "taskset": taskset, "k": k, "direction": "written by a person", "changes": changes[:2000]}
        self.state["iterations"].append(it); self.save()
        pfiles = self.parent_files(version); diff = unified_diff(pfiles, cand_files); it["diff_lines"] = diff.count("\n"); it["diff"] = diff[:20000]
        touched = {n for n in set(pfiles) | set(cand_files) if pfiles.get(n) != cand_files.get(n)}
        it["manifest"] = parse_manifest(manifest_text, task_ids, touched)
        ok, reasons, changed = critic(diff, task_ids); it["critic"] = {"ok": ok, "reasons": reasons, "changed_lines": changed}
        if not ok: it["result"] = "rejected by critic: " + "; ".join(reasons); self.save(); return it
        layer = self.build(cand_files, f"harness-{it['id']}"); it["layer"] = layer
        layers = [self.cfg["layers"][0]] + [layer] + self.cfg["layers"][2:] if len(self.cfg["layers"]) >= 2 else [layer]
        vid = self.ev("POST", "/versions", {"label": f"person {it['id']}", "layers": layers, "parent": parent_vid, "note": changes[:500]})["id"]
        it["version"] = vid; self.add_node(vid, parent_vid, note=changes[:300], iteration=it["id"], layer=layer, manifest=it["manifest"], changes=changes[:800]); self.save()
        bid = self.ev("POST", "/batches", {"kind": "compare", "taskset": taskset, "version": vid, "k": k, "tasks": core, "base_batch": base["batch"], "label": f"candidate {it['id']}"})["batch_id"]
        it["compare"] = bid; self.save(); print(f"compare batch {bid} ...", flush=True)
        b = self.wait_batch(bid)
        self.state["spent_usd"] += sum(x.get("cost") or 0 for rs in (b.get("matrix") or {}).values() for x in rs)
        it["result"] = self.judge(vid, b, base, taskset, k); it["ended"] = now(); self.save(); print(it["result"])
        self.propose_home(vid, it, self.node(vid), pfiles, cand_files); return it

    def build(self, cand_files, label):
        """Install the candidate harness on the langs layer, selfcheck, pack -> layer id."""
        langs = [l for l in self.cfg["layers"] if l][:1]      # the first default layer is agent-langs by convention (run-node.sh)
        sid = self.cp("POST", "/sandboxes", self._spec(langs, 600, []))["id"]
        try:
            self.cp("PUT", f"/sandboxes/{sid}/files?path=/tmp/cand.tar", tar_bytes(cand_files))
            self.cp("PUT", f"/sandboxes/{sid}/files?path=/tmp/launcher.sh", LAUNCHER.encode())
            r = self._exec(sid, "set -e; export PYTHONDONTWRITEBYTECODE=1; mkdir -p /aios/harness /aios/bin; rm -rf /aios/harness/aios; tar -C /aios/harness -xf /tmp/cand.tar; "
                                "find /aios/harness -name __pycache__ -prune -exec rm -rf {} +; "
                                "install -m 0755 /tmp/launcher.sh /aios/bin/aios; chown -R root:root /aios/harness /aios/bin; chmod -R a+rX /aios/harness /aios/bin; "
                                "cd /tmp && /aios/bin/aios run --help >/dev/null && /aios/runtime/python/bin/python3 -B -P -c 'import sys; sys.path.insert(0, \"/aios/harness\"); import aios.run, aios.agent, aios.model' && "
                                "find /aios/harness -name __pycache__ -prune -exec rm -rf {} +; rm -f /tmp/cand.tar /tmp/launcher.sh; echo selfcheck-ok", timeout=180)
            if "selfcheck-ok" not in (r.get("stdout") or ""): raise RuntimeError(f"candidate selfcheck failed: {(r.get('stderr') or '')[-400:]}")
            return self.cp("POST", f"/sandboxes/{sid}/pack", {"name": label}, timeout=300)["id"]
        finally:
            self.cp("DELETE", f"/sandboxes/{sid}")

    # ---- the loop
    def iterate(self, taskset, k, budget_usd, direction=None):
        ts = self.ev("GET", f"/tasksets/{taskset}"); task_ids = [t["id"] for t in ts["tasks"] if t.get("status") == "ok"]
        core = [t["id"] for t in ts["tasks"] if t.get("status") == "ok" and t.get("group") == "evolve"]
        if not core: raise RuntimeError(f"taskset {taskset} has no checked evolve tasks; run its import checks first")
        parent = self.choose_parent()
        if parent is None: raise RuntimeError("no seed node; run with --seed-version first")
        pv = self.ev("GET", "/versions"); version = next((v for v in pv["versions"] if v["id"] == parent["version"]), None)
        if not version: raise RuntimeError(f"parent version {parent['version']} not in the evaluator")
        base = self.baseline(parent["version"], taskset, k, tasks=core)
        it = {"id": f"it-{uuid.uuid4().hex[:8]}", "parent": parent["version"], "started": now(), "taskset": taskset, "k": k}
        self.state["iterations"].append(it); self.save()
        if self.state["spent_usd"] > budget_usd: it["result"] = "budget exhausted before proposing"; self.save(); return it
        parent["_direction"] = it["direction"] = DIRECTIONS[direction] if direction is not None else DIRECTIONS[(len(self.state["iterations"]) - 1) % len(DIRECTIONS)]   # recorded once, used once
        cand, pfiles, changes, usage = self.propose(parent, version, task_ids)
        parent.pop("_direction", None)
        self.state["spent_usd"] += usage.get("cost") or 0; it["propose"] = usage; it["changes"] = changes[:2000]
        diff = unified_diff(pfiles, cand); it["diff_lines"] = diff.count("\n")
        touched = {n for n in set(pfiles) | set(cand) if pfiles.get(n) != cand.get(n)}
        it["manifest"] = parse_manifest(getattr(self, "_manifest_text", ""), task_ids, touched)
        if not diff.strip(): it["result"] = "no change proposed"; self.save(); print(it["result"]); return it
        ok, reasons, changed = critic(diff, task_ids); it["critic"] = {"ok": ok, "reasons": reasons, "changed_lines": changed}
        it["diff"] = diff[:20000]
        if not ok:
            it["result"] = "rejected by critic: " + "; ".join(reasons); self.save(); print(it["result"]); return it
        layer = self.build(cand, f"harness-{it['id']}"); it["layer"] = layer
        layers = [self.cfg["layers"][0]] + [layer] + self.cfg["layers"][2:] if len(self.cfg["layers"]) >= 2 else [layer]
        vid = self.ev("POST", "/versions", {"label": f"evolve {it['id']}", "layers": layers, "parent": parent["version"], "note": changes[:500]})["id"]
        it["version"] = vid; self.add_node(vid, parent["version"], note=changes[:300], iteration=it["id"], layer=layer, manifest=it["manifest"]); self.save()
        bid = self.ev("POST", "/batches", {"kind": "compare", "taskset": taskset, "version": vid, "k": k, "tasks": core, "base_batch": base["batch"], "cascade": True, "smoke_k": 2, "label": f"evolve compare {it['id']}"})["batch_id"]
        it["compare"] = bid; self.save(); print(f"compare batch {bid} ...", flush=True)
        b = self.wait_batch(bid); st = b.get("stats") or {}
        self.state["spent_usd"] += sum(x.get("cost") or 0 for rs in (b.get("matrix") or {}).values() for x in rs)
        it["result"] = self.judge(vid, b, base, taskset, k); it["ended"] = now(); self.save(); print(it["result"])
        self.propose_home(vid, it, self.node(vid), pfiles, cand); self.file_ticket(it, b, base, taskset); return it

    def notify_update(self, vid, node, base, taskset, cand_cost, base_cost, st, extra=None):
        """docs/007 对用户的更新与回退: an accepted version is a promotion; tell the Agent service so agents following stable switch and the user hears one update."""
        if not self.cfg["intake_url"]: return
        try:
            v = self.ev("GET", f"/versions/{vid}")
            payload = update_payload(vid, v, node, base, taskset, cand_cost, base_cost, st, extra)
            Http(self.cfg["intake_url"])("POST", "/updates", payload, timeout=30)
        except Exception as e: print(f"update not delivered: {e}")  # noqa: BLE001

    def file_ticket(self, it, b, base, taskset):
        """Tell the intake layer what this iteration learned: the tasks the candidate failed most, as one evaluation Trigger."""
        if not self.cfg["intake_url"]: return
        try:
            m = b.get("matrix") or {}
            worst = sorted(((sum((r.get("score") or 0) for r in rs) / max(1, len(rs)), t) for t, rs in m.items()))[:5]
            text = (f"Iteration {it['id']} on {taskset}: {it['result']}\nWeakest tasks for the candidate: " + ", ".join(f"{t} ({s:.2f})" for s, t in worst) +
                    f"\nChanges proposed: {(it.get('changes') or '')[:800]}\nLook at the harness under /aios/harness (read-only) and the failed trajectories; propose and commit a focused improvement to the harness copy under the repository's aios/ package.")
            trig = {"id": f"evaluation-{it['id']}", "source": "evaluation", "actor": "system", "human": True, "addressed": True, "scope": "request", "text": text, "url": None,
                    "threads": [f"evaluation:{it['id']}"], "channel": {"kind": "workbench", "conversation": f"evaluation:{it['id']}"}, "meta": {"title": f"迭代 {it['id']}:{it['result'][:50]}", "iteration": it["id"], "batch": b.get("id")}}
            Http(self.cfg["intake_url"])("POST", "/triggers", trig, timeout=30)
        except Exception as e: print(f"intake trigger not filed: {e}")  # noqa: BLE001

    def propose_home(self, vid, it, node, pfiles, cand_files):
        """D29: an accepted candidate is written as a proposal under the configured harness home (EVOLVE_HOME); nothing is applied, the person runs `aios.home apply`."""
        home = self.cfg.get("home")
        if not home or not node.get("accepted"): return None
        try:
            files, deletes, code = home_proposal(pfiles, cand_files)
            d = write_proposal(home, vid, files, deletes, proposal_text(vid, it, node, files, deletes, code))
            it["proposal"] = d; self.save(); print(f"proposal written: {d}"); return d
        except Exception as e: print(f"proposal not written: {e}"); return None  # noqa: BLE001

    def judge(self, vid, b, base, taskset, k):
        """Apply the evaluator's verdict plus the cost allowance to a compare batch; the node becomes a parent when accepted."""
        st = b.get("stats") or {}; bid = b["id"]
        if b.get("status") != "done":
            node = self.node(vid); node["accepted"] = None; node["compare"] = {"batch": bid, "reason": f"compare batch ended {b.get('status')}"}
            return f"compare failed: batch {bid} ended {b.get('status')}; no verdict"
        pb = self.ev("GET", f"/batches/{base['batch']}")
        if ((pb.get("params") or {}).get("scoring"), (pb.get("params") or {}).get("taskset_version")) != ((b.get("params") or {}).get("scoring"), (b.get("params") or {}).get("taskset_version")):
            node = self.node(vid); node["accepted"] = None; node["compare"] = {"batch": bid, "reason": "the base batch was scored or priced differently, or ran other tasks; no verdict"}
            return f"no verdict: batch {bid} and its base {base['batch']} were not measured the same way (scoring or task set differs)"
        cand_cost = sum(x.get("cost") or 0 for rs in (b.get("matrix") or {}).values() for x in rs) / max(1, sum(len(rs) for rs in (b.get("matrix") or {}).values()))
        base_cost = sum(x.get("cost") or 0 for rs in (pb.get("matrix") or {}).values() for x in rs) / max(1, sum(len(rs) for rs in (pb.get("matrix") or {}).values()))
        d_cost = cand_cost - base_cost; d_score = (st.get("mean") or 0) - (base.get("mean") or 0)
        cost_ok = d_cost <= 0.01 + 0.5 * max(0.0, d_score) * max(base_cost, 0.01) * 10     # β0 = 1 cent per run, β1: each score point may buy 5× its share of the base cost
        node = self.node(vid)
        attr = attribute(node.get("manifest"), st.get("per_task") or {}, (pb.get("stats") or {}).get("per_task") or {}); node["attribution"] = attr
        guard_ok = len(attr["lost_stable"]) <= max(1, round(0.2 * attr["solved_by_parent"]))      # the regression guard: a mean that rose while tasks the parent always passed stopped being passed is not an improvement
        pareto = (not st.get("accept")) and pareto_ok(st, cand_cost, base_cost)
        node["accepted"] = (bool(st.get("accept")) and cost_ok and guard_ok) or (pareto and guard_ok)
        reason = (f"not worse (diff {(st.get('paired') or {}).get('diff'):.4f}) at {cand_cost / base_cost:.0%} of the parent's cost per run" if pareto else st.get("reason")) if cost_ok else f"cost {cand_cost:.4f}/run vs parent {base_cost:.4f}/run exceeds the allowance"
        if not guard_ok: reason = f"{len(attr['lost_stable'])} of {attr['solved_by_parent']} tasks the parent always passed are no longer passed every run; " + str(reason)
        node["compare"] = {"batch": bid, "mean": st.get("mean"), "paired": st.get("paired"), "reason": reason, "cost_per_run": cand_cost, "parent_cost_per_run": base_cost}
        for it in self.state["iterations"]:
            if it.get("version") == vid: it["attribution"] = attr
        if node["accepted"]:
            node["baseline"] = {"batch": bid, "taskset": taskset, "k": k, "status": b["status"], "mean": st.get("mean"), "delta": base.get("delta"), "progress": b.get("progress")}
            self.notify_update(vid, node, base, taskset, cand_cost, base_cost, st, extra={"kinds": {"cand": (b.get("summary") or {}).get("by_kind"), "parent": (pb.get("summary") or {}).get("by_kind")},
                                                                                       "ask": {"cand": (b.get("summary") or {}).get("ask"), "parent": (pb.get("summary") or {}).get("ask")}})
        return ("accepted" if node["accepted"] else "archived") + f": mean {st.get('mean')} vs parent {base.get('mean')}; {node['compare']['reason']}; cost/run {cand_cost:.4f} vs {base_cost:.4f}"

    def rejudge(self):
        """After a statistics change: ask the evaluator to recompute every compare batch's stats and apply the verdict again."""
        out = []
        for it in self.state["iterations"]:
            if not it.get("compare") or not it.get("version"): continue
            try: self.ev("POST", f"/batches/{it['compare']}/restat", {})
            except RuntimeError as e: out.append(f"{it['id']}: restat failed: {e}"); continue
            b = self.ev("GET", f"/batches/{it['compare']}"); parent = self.node(it["parent"]); base = (parent or {}).get("baseline")
            if not base: out.append(f"{it['id']}: parent has no baseline"); continue
            pb = self.ev("GET", f"/batches/{base['batch']}"); base["mean"] = (pb.get("stats") or {}).get("mean"); base["delta"] = (pb.get("stats") or {}).get("delta")
            it["result"] = self.judge(it["version"], b, base, it.get("taskset"), it.get("k")); out.append(f"{it['id']}: {it['result']}")
        self.save(); return out


def main(argv=None):
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--taskset", required=True); r.add_argument("--iterations", type=int, default=1); r.add_argument("--k", type=int, default=3)
    r.add_argument("--seed-version", default=None); r.add_argument("--budget-usd", type=float, default=10.0)
    r.add_argument("--base-batch", default=None, help="a baseline batch already run for the seed version on this taskset (its k and tasks are used)")
    r.add_argument("--direction", choices=sorted(DIRECTION_BY_NAME), default=None, help="aim every iteration of this run at one component instead of rotating")
    sub.add_parser("status"); sub.add_parser("rejudge"); ex = sub.add_parser("explain"); ex.add_argument("iteration", nargs="?")
    cd = sub.add_parser("candidate", help="send a candidate somebody wrote through the same road as a meta agent's"); cd.add_argument("--taskset", required=True); cd.add_argument("--k", type=int, default=2)
    cd.add_argument("--parent", required=True); cd.add_argument("--dir", required=True, help="a directory holding aios/ (the whole candidate harness)"); cd.add_argument("--changes", required=True); cd.add_argument("--manifest", required=True); cd.add_argument("--base-batch", default=None)
    a = ap.parse_args(argv); e = Evolve(cfg_from_env())
    if a.cmd == "rejudge":
        for line in e.rejudge(): print(line)
        return 0
    if a.cmd == "candidate":
        files = {}
        for root, _, names in os.walk(os.path.join(a.dir, "aios")):
            for n in names:
                if n.endswith(".pyc") or "__pycache__" in root: continue
                p = os.path.join(root, n); files[os.path.relpath(p, a.dir)] = open(p, "rb").read()
        it = e.submit_candidate(a.taskset, a.k, a.parent, files, open(a.changes).read(), open(a.manifest).read(), 0, base_batch=a.base_batch)
        print(explain(it)); return 0
    if a.cmd == "explain":
        for it in e.state["iterations"]:
            if it.get("manifest") is not None and (not a.iteration or it["id"] == a.iteration): print(f"{it['id']} ({it.get('result', '')[:60]})\n  {explain(it)}")
        return 0
    if a.cmd == "status":
        print(json.dumps({"spent_usd": round(e.state["spent_usd"], 4), "nodes": [{k: n.get(k) for k in ("version", "parent", "accepted", "note")} | {"mean": (n.get("baseline") or n.get("compare") or {}).get("mean")} for n in e.state["nodes"]],
                          "iterations": [{k: i.get(k) for k in ("id", "parent", "version", "result", "diff_lines")} for i in e.state["iterations"]]}, indent=1, ensure_ascii=False)); return 0
    if a.seed_version and not e.node(a.seed_version): e.add_node(a.seed_version, None, note="seed")
    if a.base_batch and a.seed_version:
        bb = e.ev("GET", f"/batches/{a.base_batch}"); st0 = bb.get("stats") or {}
        e.node(a.seed_version)["baseline"] = {"batch": a.base_batch, "taskset": a.taskset, "k": bb["k"], "status": bb["status"], "mean": st0.get("mean"), "delta": st0.get("delta"), "progress": bb.get("progress")}; e.save(); a.k = bb["k"]
    if not e.state["nodes"]:
        vs = e.ev("GET", "/versions")["versions"]
        if not vs: vid = e.ev("POST", "/versions", {"label": "seed", "layers": [], "note": "evaluator default layers"})["id"]
        else: vid = vs[0]["id"]
        e.add_node(vid, None, note="seed")
    for i in range(a.iterations):
        print(f"== iteration {i + 1}/{a.iterations} (spent {e.state['spent_usd']:.3f} USD)", flush=True)
        it = e.iterate(a.taskset, a.k, a.budget_usd, direction=DIRECTION_BY_NAME.get(a.direction))
        if "budget" in (it.get("result") or ""): break
    return 0


if __name__ == "__main__": sys.exit(main())
