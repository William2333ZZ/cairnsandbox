# Memory: four layers, two moments (docs/005 记忆).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError


class MemoryMixin:
    # ---- memory (docs/005 记忆): four layers held here; the harness only reads the section the host assembles and writes notes
    MEMORY_FACTS_MAX = 100

    def memory_view(self):
        rows = self.store.all("memory", order="created DESC")
        v = {layer: [r for r in rows if r["layer"] == layer] for layer in ("profile", "log", "notes", "rules")}
        v["artifacts"] = [{"ticket": t["id"], "short": t["short"], "title": t["title"], "pr": t.get("pr"), "workspace_layer": t.get("workspace_layer"), "updated": t["updated"]}
                          for t in self.store.all("tickets", order="updated DESC")[:50] if t.get("pr") or t.get("workspace_layer")]
        return v

    def memory_for(self, t):
        """What a job starts with: every profile fact, the 30 most relevant log lines (same repository first), notes of the other open jobs on it."""
        repo = self.gitea.repo if self.gitea.enabled else ""
        profile = [r["text"] for r in self.store.all("memory", "WHERE layer='profile'", order="created")][: self.MEMORY_FACTS_MAX]
        noisy = ("pass:ci", "pass:schedule", "pass:evaluation")                        # what the platform did on its own is not what this person's job needs to remember, unless this job is one of those
        log = sorted([r for r in self.store.all("memory", "WHERE layer='log'", order="created DESC") if r.get("source") not in noisy or r.get("source") == f"pass:{t.get('source')}"], key=lambda r: (0 if r.get("repo") == repo else 1, -r["created"]))[:30]
        others = [x["id"] for x in self.store.all("tickets", "WHERE state='open' AND id!=?", (t["id"],))]
        notes = [r for r in self.store.all("memory", "WHERE layer='notes'", order="created DESC") if r.get("ticket") in others and (not repo or r.get("repo") == repo)][:20]
        rules = sorted(self.store.all("memory", "WHERE layer='rules'", order="created DESC"), key=lambda r: (0 if repo and r.get("repo") == repo else 1 if not r.get("repo") else 2, -r["created"]))
        rules = [r["text"] for r in rules if not r.get("repo") or not repo or r.get("repo") == repo][:30]                  # a rule learned in another repository stays there
        return {"profile": profile, "log": [r["text"] for r in log], "notes": [{"ticket": r["ticket"], "text": r["text"]} for r in notes], "rules": rules}

    def memory_after(self, t, res):
        """The second moment: the harness's notes, one log line, and a model pass over the profile that may only remove facts it was shown."""
        repo = self.gitea.repo if self.gitea.enabled else ""
        for e in res.get("events") or []:
            if e.get("type") == "note" and re.sub(r"^\s*(outcome|question):\s*", "", str((e.get("data") or {}).get("text") or "")).strip():
                self.store.insert("memory", {"id": new_id("m"), "layer": "notes", "text": str(e["data"]["text"])[:1000], "ticket": t["id"], "repo": repo, "source": "harness", "created": now(), "updated": now()})
        rep = res.get("report") or {}
        line = f"{time.strftime('%Y-%m-%d')} {repo or 'workbench'}: {t['title'][:80]} → {rep.get('state', '?')}" + (f": {rep['text'][:120]}" if rep.get("text") else "") + (f" (${res.get('cost') or 0:.2f})" if res.get("cost") else "")
        self.store.insert("memory", {"id": new_id("m"), "layer": "log", "text": line, "ticket": t["id"], "repo": repo, "source": f"pass:{t.get('source')}", "created": now(), "updated": now()})
        if not self.proxy or any(i.get("trusted") is False for i in (t.get("inputs") or [])): return      # words from a source the person has not vouched for do not get to write the long-term profile
        facts = self.store.all("memory", "WHERE layer='profile'", order="created")[: self.MEMORY_FACTS_MAX]
        shown = {f["id"]: f["text"] for f in facts}
        notes = [r["text"] for r in self.store.all("memory", "WHERE layer='notes' AND ticket=?", (t["id"],))]
        msgs = [{"role": "system", "content": "You maintain a short profile of a software developer for their coding agent. Facts are stable preferences, conventions and long-term requirements (language, tools, review habits, repository rules). "
                                               "Reply with JSON only: {\"add\": [\"fact\", ...], \"remove\": [\"id\", ...]}. Add at most 3 facts, each under 120 characters, only if the job shows them clearly. Remove only ids listed below and only when contradicted. Nothing else."},
                {"role": "user", "content": "Current facts:\n" + "\n".join(f"{k}: {v}" for k, v in shown.items()) + f"\n\nJob: {t['title'][:200]}\nRequest: {(t.get('inputs') or [{}])[0].get('text', '')[:800]}\nOutcome: {rep.get('state')} {(rep.get('text') or '')[:400]}\nAgent notes: " + " | ".join(notes)[:800]}]
        tok = self.proxy.token(f"intake-memory-{t['short']}", 0.01)
        out = self.proxy.complete(tok, msgs, max_tokens=300).strip()
        m = re.search(r"\{.*\}", out, re.S)
        j = json.loads(m.group(0)) if m else {}
        for fid in (j.get("remove") or [])[:5]:
            if fid in shown: self.store.db.execute("DELETE FROM memory WHERE id=? AND user=?", (fid, self.store.user))
        room = self.MEMORY_FACTS_MAX - len([1 for _ in self.store.all("memory", "WHERE layer='profile'")])
        for text in (j.get("add") or [])[:3]:
            if room <= 0: break
            text = str(text).strip()[:200]
            if text and text not in shown.values(): self.store.insert("memory", {"id": new_id("m"), "layer": "profile", "text": text, "ticket": t["id"], "repo": repo, "source": "pass", "created": now(), "updated": now()}); room -= 1

    def memory_put_profile(self, facts):
        self.store.db.execute("DELETE FROM memory WHERE layer='profile' AND user=?", (self.store.user,))
        for f in facts[: self.MEMORY_FACTS_MAX]:
            text = (f.get("text") if isinstance(f, dict) else str(f)).strip()[:200]
            if text: self.store.insert("memory", {"id": (f.get("id") if isinstance(f, dict) and f.get("id") else new_id("m")), "layer": "profile", "text": text, "ticket": None, "repo": None, "source": "person", "created": now(), "updated": now()})
        return self.memory_view()

    def memory_delete(self, mid):
        if not self.store.get("memory", mid): raise KeyError(mid)
        self.store.db.execute("DELETE FROM memory WHERE id=? AND user=?", (mid, self.store.user)); return {"deleted": mid}
