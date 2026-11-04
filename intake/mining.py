# Task mining (docs/007 任务从哪来): a job whose pull request merged with tests becomes a task in the repository's own task set —
# prompt = what the person asked, workspace = the repository at the merge base, hidden tests = the PR's test files, reference =
# the PR's source changes. The evaluator checks it (reference passes, no-op fails) before it counts.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *
from clients import InfraError

TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$")
SKIP_RE = re.compile(r"(^|/)(node_modules|dist|build|images|\.git)/|\.(png|jpg|gif|pdf|zip|tar|gz|bin|so|pyc)$", re.I)


def classify_pr_files(files):
    """Changed files of a PR → (python test files, source files); deletions and binaries are left out."""
    tests, src = [], []
    for f in files:
        name = f.get("filename") or ""
        if f.get("status") == "removed" or SKIP_RE.search(name): continue
        (tests if TEST_RE.search(name) else src).append(name)
    return [t for t in tests if t.endswith(".py")], src


class MiningMixin:
    def mine_after_merge(self, t):
        """Called when a job's PR merged. Quiet when there is nothing to mine (no tests, no evaluator, no code host)."""
        if not getattr(self, "evalc", None) or not self.gitea.enabled: return None
        n = (t.get("pr") or {}).get("number")
        if not n or t.get("task"): return None
        pr = self.gitea.pull(n)
        files = self.gitea.pr_files(n)
        tests, src = classify_pr_files(files)
        if not tests or not src:
            self.store.update("tickets", t["id"], {"task": {"skipped": "no python tests" if not tests else "only tests changed"}}); return None
        base = pr.get("merge_base") or (pr.get("base") or {}).get("sha"); head = (pr.get("head") or {}).get("sha") or (t.get("pr") or {}).get("head")
        if not base or not head: return None
        tar = self.gitea.archive(base)
        if len(tar) > 60 << 20: self.store.update("tickets", t["id"], {"task": {"skipped": f"workspace {len(tar) >> 20} MiB"}}); return None
        verify = {p: self.gitea.raw(p, head).decode("utf-8", "replace") for p in tests}
        reference = {}
        for p in src[:40]:
            try: reference[p] = self.gitea.raw(p, head).decode("utf-8", "replace")
            except Exception: continue  # noqa: BLE001 — a renamed or binary file is not part of the reference
        request = next((i.get("text") for i in (t.get("inputs") or []) if i.get("kind") == "trigger"), None) or t["title"]
        prompt = (f"# {t['title'][:80]}\n\n{request.strip()}\n\nThe code is in the working directory, without git history. Make the change there; it is graded as it stands, so there is no need to commit; "
                  f"hidden tests from the original pull request will run on your tree. Keep public names and file layout.\n")
        name = "user-" + re.sub(r"[^A-Za-z0-9]+", "-", self.gitea.repo).strip("-").lower()
        tid = f"{t['short']}-{re.sub(r'[^A-Za-z0-9]+', '-', t['title'])[:40].strip('-').lower()}"
        payload = {"id": tid, "prompt": prompt, "init_tar_gz_b64": base64.b64encode(tar).decode(), "verify_files": verify, "reference_files": reference,
                   "source": {"repo": self.gitea.repo, "pr": n, "ticket": t["id"], "base": base, "head": head}, "timeout_s": 900}
        corr = [c for c in self.store.all("corrections", "WHERE ticket=? AND state='ruled'", (t["id"],)) if c.get("rule")]
        blockers = self._blockers(t)
        assigns = self.store.all("assignments", "WHERE ticket=?", (t["id"],)); elapsed = sum(a.get("elapsed_s") or 0 for a in assigns)
        long_job = len(assigns) >= 3 or elapsed >= 600 or len(src) >= 6
        if corr:                                                                                   # D17: the person corrected this job, and the PR that merged is the corrected result.
            payload["rules"] = [c["rule"] for c in corr]                                           # The prompt stays the original request: only an agent that remembers the rules can pass.
        if blockers: payload["blockers"] = blockers                                                # The agent asked and the person answered: a version that does not ask cannot pass.
        payload["kind"] = "dependent" if corr else "obstacle" if blockers else "long" if long_job else "plain"
        if long_job: payload["timeout_s"] = max(1800, min(3600, int(elapsed * 3)))
        r = self.evalc.import_task(name, payload)
        self.store.update("tickets", t["id"], {"task": {"taskset": name, "id": r.get("task", tid), "check_batch": r.get("check_batch"), "kind": payload.get("kind", "plain")}, "updated": now()})
        for c in corr: self.store.update("corrections", c["id"], {"state": "tasked", "task": {"taskset": name, "id": r.get("task", tid)}})
        self.converse("note", actor="system", text=f"这件事成了一道评测题:{name} / {r.get('task', tid)}(隐藏测试 {len(verify)} 个文件)" + (f",带着你纠正过的 {len(corr)} 条规则:只有记住它们才能做对" if corr else "") + (f",里面有 {len(blockers)} 处只有问你才知道的地方:不问就做不对" if blockers else "") + (",是一件做得久的事(按测试文件分项给分)" if long_job and not corr and not blockers else "") + ";评测器正在检查它能否区分好坏。", ticket=t["id"], meta={"task": r})
        return r

    def _blockers(self, t):
        """The places where this job needed the person: each question it asked, with what the person answered. A task made from them is
        passed only by a version that asks (docs/006: ask when stuck) -- and is scored for asking about the right things (Ask-F1)."""
        qs = self.store.all("conversation", "WHERE ticket=? AND kind='say' AND say='question'", (t["id"],), order="ts")
        ans = [i for i in (t.get("inputs") or []) if i.get("kind") == "answer" and i.get("human") and (i.get("text") or "").strip() and not (i.get("text") or "").startswith("[")]
        out = []
        for q in qs:
            text = re.sub(r"^Question:\s*", "", q["text"]).replace(AGENT_LINE, "").strip()
            nxt = next((a for a in ans if a.get("ts", 0) > q["ts"]), None)
            if text and nxt: out.append({"question": text[:600], "answer": nxt["text"].strip()[:600]})
        return out[:3]

    def mine_again(self, tid):
        t = self.store.get("tickets", tid)
        if not t: raise KeyError(tid)
        if (t.get("pr") or {}).get("state") != "merged": raise ValueError("the job's pull request is not merged")
        if isinstance(t.get("task"), dict) and t["task"].get("taskset"): return {"task": t["task"], "note": "already mined"}
        self.store.update("tickets", tid, {"task": None})
        r = self.mine_after_merge(self.store.get("tickets", tid))
        return {"task": (self.store.get("tickets", tid) or {}).get("task"), "result": r}
