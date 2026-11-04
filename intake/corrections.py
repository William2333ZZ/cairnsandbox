# Corrections (docs/001 第 6 阶段, D17): whenever a person sets the agent right -- a message that says "no, do it this way", a
# denied draft, a pull request closed without merging, a stop -- the Agent service records it, turns the words into one rule in
# memory ("记住了"), and keeps the link so that a later merged pull request becomes a dependent task (docs/007): a task whose
# success needs the rule. The harness never decides what counts as a correction; the host does, outside the thing that evolves.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *
from clients import InfraError
from rulecheck import compile_check, patch_facts, violations

CJK_RE = re.compile(r"[一-鿿]")


class CorrectionsMixin:
    MEMORY_RULES_MAX = 60

    def note_correction(self, t, kind, text="", context="", version=None):
        """Record that a person set the agent right on ticket `t`. Words (a message, a reason for a denial) are learned from in the
        background; a bare signal (PR closed, stop) is only counted."""
        text = (text or "").strip()[:1000]
        asked = bool(t.get("id")) and bool(self.store.all("conversation", "WHERE ticket=? AND kind='say' AND say='question'", (t["id"],)))
        ver = self.agent_version()
        c = {"id": new_id("c"), "ticket": t.get("id"), "kind": kind, "text": text, "context": (context or "")[:1500], "rule": None, "state": "noted", "asked_before": int(asked),
             "repo": self.gitea.repo if self.gitea.enabled else "", "version": version or ((ver or {}).get("id") if isinstance(ver, dict) else None), "task": None, "created": now()}
        self.store.insert("corrections", c)
        if text and kind in ("redirect", "denied"):
            if self.cfg.get("learn_sync") or not self.proxy: self._learn(c["id"])
            else: threading.Thread(target=self._learn, args=(c["id"],), daemon=True).start()          # the model call must not hold the Router's lock
        return c["id"]

    def _distill(self, c, t):
        """One cheap model pass: the correction in context -> (rule, scope) or (None, _) when it only concerns this one job."""
        rep = (t.get("last_report") or {}).get("text") or ""
        req = next((i.get("text") for i in (t.get("inputs") or []) if i.get("kind") == "trigger"), "") or t.get("title", "")
        msgs = [{"role": "system", "content": "A person corrected their coding agent. Write ONE rule the agent should follow next time, so the same correction is never needed twice. "
                                               "Reply with JSON only: {\"rule\": \"...\", \"scope\": \"repo\"|\"user\"|\"none\", \"check\": null|{\"kind\": \"path_forbid\"|\"new_file_regex\"|\"added_forbid\", \"pattern\": \"<python regex>\"}}. "
                                               "The rule is one imperative sentence under 160 characters in the language of the correction, "
                                               "saying what to do or avoid and when. scope=repo if it only holds for this repository, user if it is a general habit of the person, "
                                               "none if the correction only concerns this one job (a deadline, a typo, 'wait until Thursday'). "
                                               "check: ONLY when the rule can be tested on a patch -- path_forbid: no changed file path matches the regex (e.g. '^eval/'); new_file_regex: every NEW file's path must match the regex (e.g. '[a-z0-9_/]+\\.md$'); "
                                               "added_forbid: no added line matches the regex (e.g. 'print\\('). Otherwise null. Nothing else."},
                {"role": "user", "content": f"Request: {req[:600]}\nWhat the agent did or proposed ({c['kind']}): {(c.get('context') or rep)[:600]}\nThe person's correction: {c['text'][:600]}"}]
        tok = self.proxy.token(f"intake-learn-{c['id'][:8]}", 0.01)
        out = self.proxy.complete(tok, msgs, max_tokens=200).strip()
        m = re.search(r"\{.*\}", out, re.S)
        j = json.loads(m.group(0)) if m else {}
        rule = str(j.get("rule") or "").strip()[:200]
        scope = j.get("scope") if j.get("scope") in ("repo", "user", "none") else "repo"
        self._last_check = compile_check(j.get("check"))                                   # a check the model proposed and the host validated; None for a rule that is only advice
        return (None if scope == "none" or not rule else rule), scope

    def _learn(self, cid):
        c = self.store.get("corrections", cid)
        if not c or c["state"] != "noted": return
        t = self.store.get("tickets", c["ticket"]) or {"id": c["ticket"], "inputs": [], "title": ""}
        rule, scope, how = None, "repo", "model"; self._last_check = None
        if self.proxy:
            try: rule, scope = self._distill(c, t)
            except Exception as e:  # noqa: BLE001 - the correction is still worth keeping, in the person's own words
                self.problems["learn"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}; rule, how = c["text"][:160], "verbatim"; rule = (("用户纠正过:" if CJK_RE.search(rule) else "The user corrected you: ") + rule)
        else:
            rule, how = (("用户纠正过:" if CJK_RE.search(c["text"]) else "The user corrected you: ") + c["text"][:160]), "verbatim"
        if not rule:
            self.store.update("corrections", cid, {"state": "dropped"}); return None
        have = [r for r in self.store.all("memory", "WHERE layer='rules'") if r["text"] == rule]
        mid = have[0]["id"] if have else new_id("m")
        if not have:
            self.store.insert("memory", {"id": mid, "layer": "rules", "text": rule, "ticket": c["ticket"], "repo": (c.get("repo") or None) if scope == "repo" else None,
                                         "source": f"correction:{cid}", "created": now(), "updated": now(), "chk": self._last_check})
            rules = self.store.all("memory", "WHERE layer='rules'", order="created")
            for old in rules[: max(0, len(rules) - self.MEMORY_RULES_MAX)]: self.store.db.execute("DELETE FROM memory WHERE id=? AND user=?", (old["id"], self.store.user))
        self.store.update("corrections", cid, {"state": "ruled", "rule": rule})
        self.converse("note", actor="system", text=f"记住了:{rule}" + ("" if how == "model" else "(照你的原话记的)") + (",提交前我会自己检查" if self._last_check else ""), ticket=c["ticket"], meta={"correction": cid, "rule": mid, "check": self._last_check})
        return rule

    def rules_with_checks(self, t=None):
        """The person's rules that can be tested on a patch and apply here (this repository's, and the ones that hold everywhere)."""
        repo = self.gitea.repo if self.gitea.enabled else ""
        return [r for r in self.store.all("memory", "WHERE layer='rules'") if r.get("chk") and (not r.get("repo") or not repo or r.get("repo") == repo)]

    def _switch_off_slow(self, viol):
        """A check that did not finish in time is turned off for good (the rule stays as advice) and said once; the other violations go on."""
        for v in viol:
            if v.get("slow") and v.get("id"):
                self.store.update("memory", v["id"], {"chk": None}); self.problems["rulecheck"] = {"error": f"a check was too slow and was switched off: {v['rule'][:60]}", "since": now()}
        return [v for v in viol if not v.get("slow")]

    def rule_gate(self, t, rep, upd):
        """The finish gate (TRACE): a change that breaks one of the person's compiled rules does not finish. The violation goes back to the agent as an input and the job runs again
        (twice at most); after that the draft goes to the person with the violation on it (docs/009). True when a retry was set up."""
        patch = rep.get("patch")
        if not patch: return False
        rules = self.rules_with_checks(t)
        if not rules: return False
        viol = self._switch_off_slow(violations(rules, patch_facts(patch)))
        if not viol: return False
        tries = int((t.get("mandate") or {}).get("gate_retries") or 0)
        if tries >= 2: return False
        mandate = dict(t.get("mandate") or {}); mandate["gate_retries"] = tries + 1; upd["mandate"] = mandate
        text = ("Before this could finish, the host checked your change against what the person has taught you, and it breaks:\n" + "\n".join(f"- {v['rule']} ({v['why']})" for v in viol) +
                "\nFix the change so it follows these rules, then finish again.")
        upd["inputs"] = (t.get("inputs") or []) + [{"kind": "answer", "actor": "system", "text": text, "human": True, "trusted": True, "ts": now(), "delivered": False}]
        upd["attention"] = None
        self.converse("note", actor="system", text="提交前被拦下一次:违反了你教过的「" + viol[0]["rule"][:60] + "」,已让它重做。" + (f"(还有 {len(viol) - 1} 条)" if len(viol) > 1 else ""), ticket=t["id"], meta={"gate": viol})
        return True

    def corrections_view(self):
        rows = self.store.all("corrections", order="created DESC")
        for c in rows:
            t = self.store.get("tickets", c["ticket"]) or {}
            c["short"] = t.get("short"); c["title"] = t.get("title")
        return {"corrections": rows[:200], "counts": {k: sum(1 for c in rows if c["kind"] == k) for k in ("redirect", "denied", "returned", "stopped", "rollback")}}

    # ---- asking well (docs/006 该问就问, D17): the same measure the evaluator uses for obstacle tasks (Ask-F1), read off real use
    def ask_view(self):
        """Questions the agent asked and what became of them. A question the person answered with substance was worth asking; one they
        waved away ("你定") was not; a correction on a job that never asked is a question that should have been asked. Per agent version."""
        by = {}
        def bucket(v): return by.setdefault(v or "default", {"useful": 0, "unnecessary": 0, "unanswered": 0, "missed": 0})
        for t in self.store.all("tickets"):
            qs = self.store.all("conversation", "WHERE ticket=? AND kind='say' AND say='question'", (t["id"],), order="ts")
            if not qs: continue
            asg = self.store.all("assignments", "WHERE ticket=?", (t["id"],), order="created")
            ver = next((a.get("version") for a in asg if a.get("version")), None)
            ans = [i for i in (t.get("inputs") or []) if i.get("kind") == "answer" and i.get("human") and not (i.get("text") or "").startswith("[")]
            for q in qs:
                a = next((x for x in ans if x.get("ts", 0) > q["ts"]), None)
                bucket(ver)["unanswered" if not a else "unnecessary" if DISMISS_RE.match(a.get("text") or "") else "useful"] += 1
        for c in self.store.all("corrections", "WHERE kind='redirect' AND asked_before=0"):
            bucket(c.get("version"))["missed"] += 1
        def f(b):
            asked = b["useful"] + b["unnecessary"]
            p = b["useful"] / asked if asked else None
            r = b["useful"] / (b["useful"] + b["missed"]) if (b["useful"] + b["missed"]) else None
            return {**b, "precision": p, "recall": r, "f1": (2 * p * r / (p + r) if p and r else 0.0) if (p is not None and r is not None) else None}
        tot = {k: sum(b[k] for b in by.values()) for k in ("useful", "unnecessary", "unanswered", "missed")}
        return {"total": {**f(tot), "nudged": len(self.store.kv_get("intent.nudges", []) or [])}, "by_version": {v: f(b) for v, b in by.items()}}
