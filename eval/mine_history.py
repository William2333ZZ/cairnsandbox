#!/usr/bin/env python3
"""Tasks from the user's own commit history (docs/007 任务从哪来, D17): every commit that changed Python source together with Python tests is a task a
person once did in this repository -- the workspace is the repository just before the commit (the commit's test files taken out), the commit's tests are the
hidden tests, the commit's source changes are the reference, the prompt is what the commit says it did. The evaluator checks each one (the reference passes, the
untouched workspace does not) before it counts. This is how a user with years of history, or an agent with a year of merged PRs, has a task set on day one.

usage: mine_history.py --repo <git dir> --eval <url or CP url/eval> [--token T] [--taskset user-<repo>] [--max-files 8] [--max-lines 500] [--limit 40] [--dry-run]
       [--blockers N --model-url <model proxy>]   also make up to N obstacle tasks (taskset <name>-ask): the request with one decision left out, the question that would get it, the answer
The PR path (intake/mining.py) and this one produce the same kind of task; here the commit message stands in for the request, so the task is marked `origin: history`."""
import argparse, base64, io, json, os, re, subprocess, sys, tarfile, time, urllib.request

TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.py$")
SKIP_RE = re.compile(r"(^|/)(node_modules|dist|build|images|\.git|state|__pycache__)/|\.(png|jpg|gif|pdf|zip|tar|gz|bin|so|pyc|lock)$|(^|/)(package-lock\.json|pnpm-lock\.yaml)$", re.I)


def git(repo, *args, text=True):
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off", *args], capture_output=True, check=True)           # names with non-ASCII characters come as they are, not as "\\346..." in quotes
    return r.stdout.decode("utf-8", "replace") if text else r.stdout


def changed(repo, sha):
    """[(status, path, additions, deletions)] for a commit against its first parent (renames count as add + delete)."""
    names = git(repo, "diff", "--name-status", "--no-renames", f"{sha}^", sha).splitlines()
    nums = {}
    for line in git(repo, "diff", "--numstat", "--no-renames", f"{sha}^", sha).splitlines():
        a, d, p = line.split("\t", 2); nums[p] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    out = []
    for line in names:
        st, p = line.split("\t", 1); out.append((st[0], p, *nums.get(p, (0, 0))))
    return out


def classify(files):
    """-> (tests, sources, lines): Python tests and Python sources that were added or modified; None when the commit is not a task."""
    tests = [p for st, p, a, d in files if st in "AM" and p.endswith(".py") and TEST_RE.search(p)]
    src = [p for st, p, a, d in files if st in "AM" and not TEST_RE.search(p) and not SKIP_RE.search(p)]
    py_src = [p for p in src if p.endswith(".py")]
    return tests, src, py_src, sum(a + d for st, p, a, d in files if not SKIP_RE.search(p))


def candidates(repo, max_files=8, max_lines=500):
    """Oldest first. A commit qualifies with >=1 Python test file and >=1 Python source file, within the size limits."""
    out = []
    for sha in git(repo, "log", "--reverse", "--no-merges", "--format=%H").split():
        try: files = changed(repo, sha)
        except subprocess.CalledProcessError: continue                         # the root commit has no parent
        tests, src, py_src, lines = classify(files)
        if not tests or not py_src: continue
        if len([1 for st, p, a, d in files if not SKIP_RE.search(p)]) > max_files or lines > max_lines: continue
        out.append({"sha": sha, "tests": tests, "src": src, "lines": lines, "files": len(files)})
    return out


def message(repo, sha):
    subject, _, body = git(repo, "show", "-s", "--format=%s%n%x00%b", sha).partition("\x00")
    body = re.sub(r"\n?Co-Authored-By:.*", "", body, flags=re.I).strip()
    return subject.strip(), body


def hints_for(repo, sha, verify, blocker=None):
    """D24: the names the tests use that the workspace lacks, as prompt text (empty when there are none). For an obstacle variant a line that would give away the
    left-out decision is dropped."""
    from interfaces import missing_interfaces
    paths = git(repo, "ls-tree", "-r", "--name-only", f"{sha}^").splitlines()
    added = [p for st, p, a, d in changed(repo, sha) if st == "A" and p.endswith(".py") and not TEST_RE.search(p)]
    def read(p):
        try: return git(repo, "show", f"{sha}^:{p}")
        except subprocess.CalledProcessError: return ""
    lines = missing_interfaces({p: t for p, t in verify.items() if p.endswith(".py")}, paths, read, added)
    if blocker:                                                                                 # a name can carry the decision (`retry_limit`, `RetryLimit`): compare without case or underscores; an answer with no plain words (a number, Chinese) cannot be checked, so no hints
        flat = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())                                    # noqa: E731
        words = [flat(w) for w in re.findall(r"[A-Za-z0-9_]{2,}", str(blocker.get("answer", "")))]
        if not words: return ""
        lines = [l for l in lines if not any(w and w in flat(l) for w in words)]
    return ("\n\nWhat the checks use that does not exist in the code yet (use exactly these names):\n" + "\n".join("- " + l for l in lines)) if lines else ""


def task_body(repo, c, group, blocker=None):
    sha = c["sha"]; subject, body = message(repo, sha)
    tar = git(repo, "archive", "--format=tar.gz", "--prefix=repo/", f"{sha}^", text=False)
    verify = {p: git(repo, "show", f"{sha}:{p}") for p in c["tests"]}
    reference = {}
    for p in c["src"][:40]:
        try: reference[p] = git(repo, "show", f"{sha}:{p}")
        except subprocess.CalledProcessError: continue
    request = blocker["request"] if blocker else subject + (("\n\n" + body[:1500]) if body else "")
    request += hints_for(repo, sha, verify, blocker)
    prompt = (f"# {subject[:80] if not blocker else blocker['request'][:80]}\n\n{request}\n\nThe code is in the working directory, without git history. Make the change there; it is graded as it stands, so there is no need to commit; "
              f"hidden tests from the original change will run on your tree. Keep public names and file layout.\n")
    out = {"id": f"{'o' if blocker else 'h'}-{sha[:8]}", "prompt": prompt, "init_tar_gz_b64": base64.b64encode(tar).decode(), "verify_files": verify, "reference_files": reference, "group": group,
           "kind": "obstacle" if blocker else "long" if len(c["src"]) >= 6 else "plain", "timeout_s": 1800 if len(c["src"]) >= 6 else 900,
           "source": {"origin": "history+synthetic-blocker" if blocker else "history", "commit": sha, "subject": subject[:120]}}
    if blocker and blocker.get("rule"):                                                                  # a dependent variant: the decision is a rule the agent is handed, not a question it must ask
        out.update({"id": f"d-{sha[:8]}", "kind": "dependent", "rules": [blocker["rule"]], "source": {**out["source"], "origin": "history+synthetic-rule"}}); out.pop("blockers", None)
    elif blocker: out["blockers"] = [{"question": blocker["question"], "answer": blocker["answer"]}]
    return out


def blocker_for(repo, c, llm, why=None):
    """An obstacle variant of a history task (docs/007 obstacle; HiL-Bench's recipe, made by a model from the person's own commit): the request minus ONE decision the change fixed and the
    repository cannot tell you, the question that would get it, and the answer. Grounded: the answer must come from the commit, and the reduced request must not give it away. None when the model cannot."""
    subject, body = message(repo, c["sha"])
    diff = git(repo, "show", "--format=", "--no-color", c["sha"], "--", *c["src"][:6])[:6000]
    out = llm([{"role": "system", "content": "From a real code change, write a TASK where one decision is missing, for testing whether an agent asks. Reply with JSON only: "
                                              "{\"request\": \"what a busy person would write, WITHOUT one specific decision (a value, a name, a choice between options) that the change fixed and the repository could not tell you\", "
                                              "\"question\": \"the one question a good agent asks to get it\", \"answer\": \"the answer the person gives, a short fact that appears in the change\"}. "
                                              "The request must not contain the answer. Every change fixes something specific -- a number, a default, a name, a path, a format, which of two options -- so pick the single most consequential one; "
                                              "the answer must be a fact a person would know and the repository would not show before the change."},
               {"role": "user", "content": f"Commit message:\n{subject}\n{body[:800]}\n\nChange (source files):\n{diff}"}])
    m = re.search(r"\{.*\}", out or "", re.S); why = why if why is not None else []
    try: j = json.loads(m.group(0)) if m else {}
    except ValueError: why.append("not json"); return None
    req, q, a = (str(j.get(k) or "").strip() for k in ("request", "question", "answer"))
    if not (req and q and a) or len(a) > 200: why.append("the model found no decision to leave out" if not req else "incomplete"); return None
    grounded = {w for w in re.findall(r"[A-Za-z0-9_.\-]{3,}|[\u4e00-\u9fff]{2,}", a)}
    haystack = (subject + body + diff).lower()
    if grounded and sum(1 for w in grounded if w.lower() in haystack) / len(grounded) < 0.5: why.append(f"answer not in the change: {a[:40]}"); return None      # made up, not in the change
    if a.lower() in req.lower() or (grounded and all(w.lower() in req.lower() for w in grounded)): why.append("the request gives the answer away"); return None  # the request gives it away
    return {"request": req[:600], "question": q[:400], "answer": a}


def dependent_for(repo, c, llm, why=None):
    """A dependent variant of a history task (docs/007 dependent): the same reduced request, but the missing decision arrives as a rule the person once taught -- what memory would hand the agent.
    Passing it needs the rule (the request alone leaves the decision out); running the same task with the rule held back is the control that measures what memory is worth."""
    b = blocker_for(repo, c, llm, why)
    if not b: return None
    out = llm([{"role": "system", "content": "Turn a question and its answer into ONE standing rule a person taught their coding agent earlier, so the agent need not ask again. Imperative, under 160 characters, in the language of the answer, "
                                              "stating the fact as a convention of this repository (not as a reply). Reply with the rule only."},
               {"role": "user", "content": f"Question: {b['question']}\nAnswer: {b['answer']}"}]).strip().strip('"')
    if not out or len(out) > 200: return None
    return {**b, "rule": out}


def split_groups(n):
    """Chronological 60 / 20 / 20: later commits are held out, so a version is never evaluated on work that came after what it was shaped by."""
    return ["evolve" if i < int(n * 0.6 + 0.5) else "validate" if i < int(n * 0.8 + 0.5) else "test" for i in range(n)]


def model_caller(url, model):
    """A function messages -> text, through the model proxy with a small budget of its own (the proxy hands out a token per name)."""
    base = url.rstrip("/"); tok = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/tokens", data=json.dumps({"sandbox": "mine-history", "budget_usd": 1.0}).encode(), method="POST", headers={"Content-Type": "application/json"}), timeout=30).read())["token"]
    def call(messages):
        r = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps({"model": model, "messages": messages, "max_tokens": 2500, "temperature": 0.6}).encode(), method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
        return json.loads(urllib.request.urlopen(r, timeout=120).read())["choices"][0]["message"]["content"]
    return call


class Http:
    def __init__(self, base, token=""): self.base, self.token = base.rstrip("/"), token
    def post(self, path, body, timeout=300):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as x: return x.status, json.loads(x.read())
        except urllib.error.HTTPError as e: return e.code, {"error": e.read()[:300].decode("utf-8", "replace")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True); ap.add_argument("--eval", default=os.environ.get("EVAL_URL", "")); ap.add_argument("--token", default=os.environ.get("CP_TOKEN", ""))
    ap.add_argument("--taskset", default=None); ap.add_argument("--max-files", type=int, default=8); ap.add_argument("--max-lines", type=int, default=500)
    ap.add_argument("--limit", type=int, default=40); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--skip", default="", help="comma-separated short shas to leave out")
    ap.add_argument("--blockers", type=int, default=0); ap.add_argument("--only-blockers", action="store_true", help="the plain tasks are already imported; make only the obstacle variants"); ap.add_argument("--dependents", type=int, default=0, help="also make up to N dependent tasks (taskset <name>-mem)"); ap.add_argument("--model-url", default=os.environ.get("MODEL_PROXY_URL", "")); ap.add_argument("--model-name", default="deepseek-flash")
    a = ap.parse_args(argv)
    name = a.taskset or "user-" + re.sub(r"[^A-Za-z0-9]+", "-", os.path.basename(os.path.abspath(a.repo))).strip("-").lower()
    cs = [c for c in candidates(a.repo, a.max_files, a.max_lines) if c["sha"][:8] not in a.skip.split(",")][-a.limit:]
    groups = split_groups(len(cs))
    for c, g in zip(cs, groups): print(f"{c['sha'][:8]} {g:8} {len(c['tests'])} tests, {len(c['src'])} src, {c['lines']:4d} lines  {message(a.repo, c['sha'])[0][:70]}")
    if a.dry_run or not a.eval: print(f"{len(cs)} candidate commits -> taskset {name}"); return 0
    ev = Http(a.eval, a.token); ok = 0
    for c, g in zip(cs, groups):
        if a.only_blockers: break
        body = task_body(a.repo, c, g)
        code, r = ev.post(f"/tasksets/{name}/import", body)
        print(f"{c['sha'][:8]} -> {code} {r.get('task') or r.get('error')}", flush=True); ok += code == 201
    print(f"imported {ok}/{len(cs)} into {name}; each is checked by the evaluator (reference passes, no-op fails) before it counts")
    if a.dependents and a.model_url:
        llm = model_caller(a.model_url, a.model_name); made = 0
        for c, g in zip(cs, groups):
            if made >= a.dependents: break
            why = []
            try: b = dependent_for(a.repo, c, llm, why)
            except Exception as e: print(f"{c['sha'][:8]} dependent: {type(e).__name__}: {e}"); continue  # noqa: BLE001
            if not b: print(f"{c['sha'][:8]} dependent: none ({'; '.join(why)})"); continue
            code, r = ev.post(f"/tasksets/{name}-mem/import", task_body(a.repo, c, g, b)); made += code == 201
            print(f"{c['sha'][:8]} dependent -> {code} {r.get('task') or r.get('error')} | rule: {b['rule'][:90]}", flush=True)
        print(f"made {made} dependent tasks in {name}-mem")
    if a.blockers and a.model_url:
        llm = model_caller(a.model_url, a.model_name); made = 0
        for c, g in zip(cs, groups):
            if made >= a.blockers: break
            why = []
            try: b = blocker_for(a.repo, c, llm, why)
            except Exception as e: print(f"{c['sha'][:8]} blocker: {type(e).__name__}: {e}"); continue  # noqa: BLE001
            if not b: print(f"{c['sha'][:8]} blocker: none ({'; '.join(why)})"); continue
            code, r = ev.post(f"/tasksets/{name}-ask/import", task_body(a.repo, c, g, b)); made += code == 201
            print(f"{c['sha'][:8]} obstacle -> {code} {r.get('task') or r.get('error')} | ask: {b['question'][:70]} | answer: {b['answer'][:50]}", flush=True)
        print(f"made {made} obstacle tasks in {name}-ask")
    return 0


if __name__ == "__main__": sys.exit(main())
