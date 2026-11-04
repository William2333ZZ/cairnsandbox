# Intent watch (docs/006 该问就问, D17; "Ask or Assume?", Edwards & Schuster 2026): a coding agent that cannot tell what is missing keeps exploring until its
# steps run out (the first real asking walk did exactly that). The paper's remedy is a second reader that watches task state and says when to ask, apart from
# the agent that edits and runs -- 69% resolved against 61% for one agent with an ask tool, and fewer questions on easy tasks. Here the second reader is the host:
# at two points in a long job it reads the request and the last steps with its own model call and, only when the job waits on something only the person can
# say, tells the agent to ask exactly that. It cannot be edited by the version loop, and it asks nothing itself: the agent's QUESTION is what reaches the person.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *
from clients import InfraError

INTENT_MARKS = (10, 24)          # model calls into a job at which the watch looks


class IntentMixin:
    def intent_check(self, tid, sid, n_calls):
        """Called by the watchdog for each running job. At most one look per mark per job; returns the question when it nudged, else None."""
        if not self.proxy or not n_calls: return None
        seen = self.__dict__.setdefault("_intent_seen", {})
        due = [m for m in INTENT_MARKS if n_calls >= m and (tid, m) not in seen]
        if not due: return None
        seen[(tid, due[-1])] = now(); [seen.setdefault((tid, m), now()) for m in due]
        t = self.store.get("tickets", tid)
        if not t or t.get("attention"): return None
        live = self.live(tid); msgs = [m for m in live.get("messages", []) if m.get("role") in ("assistant", "user", "tool")]
        if len(msgs) < 6: return None
        def clip(m): c = m.get("content"); c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False); return f"[{m.get('role')}] {c[:500]}"
        req = next((i.get("text") for i in (t.get("inputs") or []) if i.get("kind") == "trigger"), "") or t["title"]
        answered = [i.get("text") for i in (t.get("inputs") or []) if i.get("kind") == "answer" and i.get("actor") != "host"]
        prompt = [{"role": "system", "content": "You watch a coding agent work for a person. Decide whether the agent is stuck on something only the PERSON can supply: a value, a choice, a preference, "
                                                  "a detail the request leaves out -- not something it can find by reading the repository or running commands. If it already decided and is making progress, say no. "
                                                  "Reply with JSON only: {\"missing\": true|false, \"question\": \"the one question to ask the person, in the language of the request\"}."},
                  {"role": "user", "content": f"Request: {req[:800]}\n" + (f"Already answered by the person: {' | '.join(a[:200] for a in answered)}\n" if answered else "") + f"Steps so far: {n_calls}\nLast steps:\n" + "\n".join(clip(m) for m in msgs[-8:])}]
        try:
            tok = self.proxy.token(f"intake-intent-{tid[-8:]}-{due[-1]}", 0.02)
            out = self.proxy.complete(tok, prompt, max_tokens=200).strip(); m = re.search(r"\{.*\}", out, re.S); j = json.loads(m.group(0)) if m else {}
        except Exception as e:  # noqa: BLE001
            self.problems["intent"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}; return None
        q = str(j.get("question") or "").strip()[:300]
        if not j.get("missing") or not q: return None
        try:
            self.cp.inbox(sid, {"kind": "answer", "actor": "host", "human": True, "text": f"[host] You have been working for {n_calls} steps and the request seems to depend on something only the person can tell you: {q}\n"
                                                                                       "Stop exploring. Finish now with a QUESTION asking exactly this, instead of guessing."})
        except InfraError: return None
        self.store.kv_set("intent.nudges", ((self.store.kv_get("intent.nudges", []) or []) + [{"ticket": tid, "at": now(), "calls": n_calls, "question": q}])[-100:])
        return q
