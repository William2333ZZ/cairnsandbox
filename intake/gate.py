# Eval: Gate, Assessment, Decision, accept, revalidation (docs/005 两条路).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError


class GateMixin:
    # ---- eval
    def evaluate_due(self):
        with self.lock: due = [th for th in self.store.all("threads") if th.get("eval") and (not th["eval"].get("running") or now() - th["eval"]["running"] > 600) and self._merge_elapsed(th)]
        for th in due:
            if self.paused: return
            with self.lock:
                th = self.store.get("threads", th["id"])
                if not th.get("eval") or (th["eval"].get("running") and now() - th["eval"]["running"] <= 600): continue
                ev = th["eval"]; ev["running"] = now(); ev["attempts"] = ev.get("attempts", 0) + 1; self.store.update("threads", th["id"], {"eval": ev})
                if ev["attempts"] > 3:
                    self._decide(th["id"], ev["triggers"], "unavailable", None, "timeout", reason="assessment did not finish three times")
                    self.store.update("threads", th["id"], {"eval": None}); continue
            try: self._eval(th, ev)
            except Exception as e:  # noqa: BLE001
                self.problems["eval"] = {"error": f"{type(e).__name__}: {e}"[:300], "since": now()}
                with self.lock: self._decide(th["id"], ev["triggers"], "unavailable", None, "exception", reason=str(e)[:300])
            finally:
                with self.lock:
                    th2 = self.store.get("threads", th["id"]); ev2 = th2.get("eval") or {}
                    rest = [x for x in ev2.get("triggers", []) if x not in ev["triggers"]]
                    self.store.update("threads", th["id"], {"eval": {"triggers": rest, "queued": now()} if rest else None, "updated": now()})

    def _merge_elapsed(self, th):
        """Router merge: wait merge_s after the last human trigger on the thread so consecutive messages become one batch."""
        ev = th.get("eval") or {}
        if ev.get("running") or not self.cfg.get("merge_s") or not ev.get("triggers"): return True
        trig = self.store.get("triggers", ev["triggers"][-1]) or {}
        if not trig.get("human"): return True
        return now() - (trig.get("created") or 0) >= self.cfg["merge_s"]

    def _eval(self, thread, ev):
        trigs = [self.store.get("triggers", x) for x in ev["triggers"]]
        trigs = [t for t in trigs if t]
        if not trigs: return
        has_tickets = any(self.store.get("tickets", x) for x in self.store_tickets_on(thread["id"]))
        head = trigs[-1]                                                  # the gate judges the newest human message of the batch
        humans = [t for t in trigs if t["human"]]
        if humans: head = humans[-1]
        strength, basis = self.policy.gate(head, has_tickets)
        if strength is None:
            with self.lock: self._decide(thread["id"], ev["triggers"], "ignore", None, basis, reason="not admitted by the gate"); return
        text = "\n".join(t["text"] for t in trigs) + self._related_text(thread)
        if head["source"] in ("ci", "schedule", "evaluation") and self.over_daily_budget():                # 001 第 1 阶段: what it does on its own initiative has a daily limit; what a person asks for does not
            spent, cap = self.spent_today(), (self.policy.p.get("defaults") or {}).get("daily_budget_usd") or 0
            why = "the model provider refused for balance" if self.billing_blocked() and not self.over_cap() else f"today's spend ${spent:.2f} reached the daily limit ${cap:.2f}"
            with self.lock:
                self._decide(thread["id"], ev["triggers"], "ignore", strength, "daily-budget", reason=f"{why}; automatic work waits")
                if self.store.kv_get("budget.noted") != time.strftime("%Y-%m-%d") and not (self.billing_blocked() and not self.over_cap()):
                    self.store.kv_set("budget.noted", time.strftime("%Y-%m-%d"))
                    self.converse("note", actor="system", text=f"今天已经花了 ${spent:.2f},到了每日上限 ${cap:.2f}:CI、例行这类它自己发起的事先停着,你开口要的事照常做。")
            return
        if STOP_RE.match(head["text"] or "") and has_tickets and strength == "explicit":
            with self.lock:
                for tid in self.store_tickets_on(thread["id"]): self.stop_ticket(tid, by="person", reason=f"stop by {head['actor']}")
                self._decide(thread["id"], ev["triggers"], "stop", strength, basis, reason="person asked to stop")
            return
        if strength == "explicit":
            with self.lock: self._accept(thread, trigs, head, strength, basis, matched=[], by="gate")
            return
        rules = self.policy.enabled_rules()
        if not rules:
            with self.lock: self._decide(thread["id"], ev["triggers"], "ignore", strength, basis, reason="no enabled rules for proactive work"); return
        matched, reason = self.assess(thread, text, rules)
        would = bool(matched)
        with self.lock:
            if would and self.policy.p["mode"] == "auto": self._accept(thread, trigs, head, strength, basis, matched=matched, by="assessment")
            else: self._decide(thread["id"], ev["triggers"], "ignore", strength, basis, reason=("would accept: " if would else "") + reason, matched=matched, would=would)

    def _related_text(self, thread):
        """What the Threads correlated with this one said lately (docs/001 Correlation): context for the judgement, never a reason to route."""
        lines = []
        for oth in (thread.get("correlated") or [])[:5]:
            for tid in reversed(self.store.all("triggers", "WHERE threads LIKE ?", (f"%{oth}%",))[-3:]):
                if tid.get("human"): lines.append(f"[{oth}] {tid.get('actor')}: {(tid.get('text') or '')[:300]}")
        return ("\n\nRelated conversations (context only):\n" + "\n".join(lines[:8])) if lines else ""

    def assess(self, thread, text, rules):
        """The model judges every enabled rule once. Returns (matched rule names, reason). Uses the intake layer's own token."""
        if not self.proxy: return [], "no model proxy"
        tok = self.proxy.token(f"intake-assess-{thread['id'][:40]}", 0.01)
        rl = "\n".join(f"- {r['name']}: {r['text']}" for r in rules)
        msgs = [{"role": "system", "content": "You decide whether an automated coding agent should start work on a conversation. Judge each rule. "
                                               "Answer with exactly one line: MATCH <rule names separated by spaces> | <short reason>  or  NONE | <short reason>."},
                {"role": "user", "content": f"Rules:\n{rl}\n\nConversation ({thread['id']}):\n{text[:6000]}"}]
        out = self.proxy.complete(tok, msgs).strip().splitlines()
        line = out[0] if out else "NONE | empty answer"
        names = {r["name"] for r in rules}
        if line.upper().startswith("MATCH"):
            head, _, reason = line.partition("|"); matched = [w for w in head.split()[1:] if w in names]
            return matched, reason.strip() or "matched"
        return [], line.partition("|")[2].strip() or line[:200]

    def _decide(self, thread_id, trig_ids, outcome, strength, basis, reason="", matched=None, would=False, by="gate", ticket=None):
        d = {"id": new_id("d"), "thread": thread_id, "triggers": trig_ids, "outcome": outcome, "strength": strength, "basis": basis, "reason": reason,
             "matched_rules": matched or [], "would_accept": would, "policy_version": self.policy.version, "by_whom": by, "ticket": ticket, "created": now()}
        self.store.insert("decisions", d)
        for x in trig_ids:
            self.store.update("triggers", x, {"disposition": f"decided:{outcome}"})
            if outcome != "accept": self._handoff_signal(self.store.get("triggers", x) or {}, "refused", reason or basis)
        return d

    def _accept(self, thread, trigs, head, strength, basis, matched, by):
        defaults = self.policy.p["defaults"]
        tid = new_id("t"); short = short_name(tid)
        mandate = {"policy_version": self.policy.version, "scope": defaults.get("scope"), "strength": strength, "basis": basis, "matched_rules": matched,
                   "actor": head["actor"], "source": head["source"], "received_as": head.get("scope"), "budget_usd": float((head.get("meta") or {}).get("budget_usd") or defaults["budget_usd"]),
                   "network": defaults.get("network", "allowlist"), "version": (head.get("meta") or {}).get("version") or self.agent_version() or defaults.get("version"), "accepted": now(),
                   "handoff": (head.get("meta") or {}).get("handoff")}
        t = {"id": tid, "short": short, "title": (thread.get("title") or head["text"][:80]), "thread": thread["id"], "source": head["source"], "mandate": mandate,
             "channels": [head["channel"]] if head.get("channel") else [], "inputs": [], "state": "open", "ended_by": None, "outcome": None, "workspace_layer": None,
             "branch": f"ticket/{short}", "pr": None, "attention": None, "last_report": None, "created": now(), "updated": now()}
        t["inputs"] = [{"kind": "trigger", "trigger": x["id"], "actor": x["actor"], "text": x["text"], "human": x["human"], "ts": x["created"], "delivered": False, "trusted": self.trusted(x)} for x in trigs]
        self.store.insert("tickets", t)
        self.converse("work", ticket=tid, text=t["title"], meta={"short": short, "source": head["source"]})
        subs = list(thread.get("subscriptions") or []) + [tid]
        self.store.update("threads", thread["id"], {"subscriptions": subs, "updated": now()})
        d = self._decide(thread["id"], [x["id"] for x in trigs], "accept", strength, basis, reason="accepted", matched=matched, by=by, ticket=tid)
        for x in trigs: self._handoff_signal(x, "accepted", tid)                              # a job handed over by another agent: tell the giver it is taken
        rules = [r for r in self.policy.enabled_rules() if r["name"] in matched]
        over = self.over_daily_budget()
        self._ack(t, f"Taking this on as ticket `{short}`: {head['text'][:120].strip()}." + (f" (Today's spend ${self.spent_today():.2f} is over the daily limit; this one you asked for, so it goes ahead.)" if over else "") + f" {AGENT_LINE}" if not any(r.get('silent') for r in rules) else None)
        self._new_assignment(tid, recovery=False)
        return d

    def _ack(self, ticket, text):
        if text: self.say(ticket, "ack", text)
