# Receiving: dedupe, ownership, merge, stop, rollback, routine drafts; the person's actions on a job (docs/005 对话与路由).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError
from sources import workbench_trigger


class RouterMixin:
    # ---- triggers
    def receive(self, trigger):
        """Store a Trigger once; fan it out to subscribed Tickets; otherwise queue it for Eval on its primary Thread."""
        with self.lock:
            if self.store.get("triggers", trigger["id"]): return "duplicate"
            if self.gitea.enabled and trigger["source"] == getattr(self.gitea, "kind", "gitea") and trigger["actor"] == (self.gitea.bot or ""): return "filtered"
            t = {"disposition": "stored", "created": now(), **trigger}
            self.store.insert("triggers", t)
            for th in trigger["threads"]:
                self._ensure_thread(th, trigger)
            primary = trigger["threads"][0]
            self._correlate(trigger["threads"])
            if trigger.get("human") or trigger.get("actor") == "system":                 # Conversation: every message the user (or the platform) says, once, with where it came from
                self.converse("message", actor=trigger["actor"], text=trigger.get("text") or "", origin={"source": trigger["source"], "thread": primary, "url": trigger.get("url"), "trigger": trigger["id"]})
            thread = self.store.get("threads", primary)
            owners = self._owners(trigger["threads"])
            if owners and trigger["human"] and STOP_RE.match(trigger["text"] or "") and self.policy.is_allowed(trigger["source"], trigger["actor"]):
                live = [tk for tk in owners if (self.store.get("tickets", tk) or {}).get("state") == "open"]
                for tk in live: self.stop_ticket(tk, by="person", reason=f"stop by {trigger['actor']}")
                self._decide(primary, [trigger["id"]], "stop", "explicit", "listed-stop", reason=f"person asked to stop ({len(live)} live ticket(s))")
                return "stopped"
            if trigger.get("human") and trigger.get("addressed") and trigger.get("actor") != "system" and ROLLBACK_RE.match(trigger.get("text") or "") and self.policy.is_allowed(trigger["source"], trigger["actor"]):
                was = self.agent_version()
                r = self.set_agent_version("previous", pin=True)
                self._decide(primary, [trigger["id"]], "ignore", "explicit", "rollback", reason="rolled the agent back"); self.store.update("triggers", trigger["id"], {"disposition": "rollback"})
                self.converse("note", actor="system", text=f"已退回到 {((r.get('version') or {}).get('label') or (r.get('version') or {}).get('id') or '上一版')},并钉住;之后的更新不再自动换上。" + (f"退掉的是 {was.get('label') or was.get('id')}。" if was and r.get("changed") else "") if r.get("version") else "没有可退回的版本。", origin={"source": trigger["source"], "thread": primary})
                if was and r.get("changed"): self.note_correction({"id": None}, "rollback", trigger.get("text") or "", context=f"rolled back from {was.get('id')} ({was.get('label')})", version=was.get("id"))      # the person judged an update and took it back
                return "rollback"
            if trigger.get("human") and trigger.get("addressed") and not owners:                      # a word about the agent itself (budget, "what are you connected to"): answered here, never a job
                reply = self.command(trigger)
                if reply:
                    self._decide(primary, [trigger["id"]], "ignore", "explicit", "command", reason="a command about the agent itself"); self.store.update("triggers", trigger["id"], {"disposition": "command"})
                    self.converse("note", actor="system", text=reply, origin={"source": trigger["source"], "thread": primary})
                    return "command"
            sc = parse_routine(trigger.get("text")) if trigger.get("human") and trigger.get("addressed") and trigger.get("actor") != "system" and not owners and self.policy.is_allowed(trigger["source"], trigger["actor"]) else None   # the routine's own firings are jobs
            if sc:                                                                       # recurring work is a routine draft, not a job (docs/005 例行)
                pseudo = {"id": None, "short": "routine", "title": sc["text"][:80], "channels": [trigger["channel"]] if trigger.get("channel") else []}
                when = f"每天 {sc['daily_at']}" if sc.get("daily_at") else f"每 {sc['every_s'] // 60} 分钟" if sc.get("every_s", 0) < 3600 else f"每 {sc['every_s'] // 3600} 小时"
                self._approval(pseudo, "routine", {"schedule": sc, "trigger": trigger["id"]}, f"新建例行({when}):{sc['text'][:200]}")
                self._decide(primary, [trigger["id"]], "ignore", "explicit", "routine-draft", reason="proposed as a routine; waiting for consent")
                self.store.update("triggers", trigger["id"], {"disposition": "routine-draft"})
                return "routine-draft"
            if owners and trigger["human"]:
                taken = [tk for tk in owners if self._fan_out(tk, trigger)]
                if taken:
                    self.store.update("triggers", trigger["id"], {"disposition": "fanned-out"})
                    return "fanned-out"
            if owners:                                   # bot/CI messages: context only
                for tk in owners:
                    if (self.store.get("tickets", tk) or {}).get("state") == "open": self._fan_out(tk, trigger)
            for tk in self._context_tickets(trigger["threads"], exclude=owners):      # docs/001 订阅: a Thread a Ticket only reads. Its messages arrive as context; they never make the Ticket theirs, wake an ended one, or count as taken
                self._fan_out(tk, trigger, context=True)
            ev = thread.get("eval") or {"triggers": [], "queued": now()}
            ev["triggers"].append(trigger["id"]); ev["queued"] = ev.get("queued") or now()
            self.store.update("threads", primary, {"eval": ev, "updated": now()})
            self.store.update("triggers", trigger["id"], {"disposition": "queued"})
            return "queued"

    def trusted(self, trigger):
        """Whether the words are the person's (or the platform's own): the workbench, a listed person, the system. A CI bot, an unlisted commenter or a table-admitted
        stranger is data about the task, not an instruction (docs/009); the executor wraps it so and the review looks harder at what it led to."""
        if (trigger.get("meta") or {}).get("tainted"): return False                    # a hand-off that carries words from an unvouched source stays untrusted
        if trigger.get("actor") == "system" or trigger.get("source") == "workbench": return True
        return bool(trigger.get("human")) and self.policy.is_allowed(trigger.get("source", ""), trigger.get("actor", ""))

    def _ensure_thread(self, th, trigger):
        if not self.store.get("threads", th):
            self.store.insert("threads", {"id": th, "source": trigger["source"], "title": (trigger.get("meta") or {}).get("title") or trigger["text"][:80],
                                          "subscriptions": [], "context": [], "correlated": [], "eval": None, "closed": None, "created": now(), "updated": now()})

    def _owners(self, thread_ids):
        """Tickets that own a message on any of these Threads: open, or ended in a way a person may resume."""
        out = []
        for th in thread_ids:
            for tk in (self.store.get("threads", th) or {}).get("subscriptions") or []:
                if tk not in out and ((self.store.get("tickets", tk) or {}).get("state") in ("open",) or self._resumable(tk)): out.append(tk)
        return out

    def _context_tickets(self, thread_ids, exclude=()):
        out = []
        for th in thread_ids:
            for tk in (self.store.get("threads", th) or {}).get("context") or []:
                if tk not in out and tk not in exclude and (self.store.get("tickets", tk) or {}).get("state") == "open": out.append(tk)
        return out

    def _correlate(self, thread_ids):
        """Correlation (docs/001): Threads that share a Trigger are related. It adds context for Eval and routes nothing."""
        for a in thread_ids:
            others = [b for b in thread_ids if b != a]
            th = self.store.get("threads", a) or {}; have = th.get("correlated") or []
            new = [b for b in others if b not in have]
            if new: self.store.update("threads", a, {"correlated": (have + new)[-20:]})

    def subscribe(self, tid, thread_id, mode="own", source=None):
        """A Ticket follows a Thread: `own` (its people's messages there are the Ticket's to answer or hand back, and may resume it) or `context` (it reads, nothing more).
        Idempotent. The Thread is correlated with the Ticket's own, so Eval sees them together."""
        if mode not in ("own", "context"): raise ValueError("mode is own or context")
        t = self.store.get("tickets", tid)
        if t is None: raise KeyError("unknown ticket")
        with self.lock:
            if not self.store.get("threads", thread_id):
                self.store.insert("threads", {"id": thread_id, "source": source or t["source"], "title": thread_id, "subscriptions": [], "context": [], "correlated": [], "eval": None, "closed": None, "created": now(), "updated": now()})
            th = self.store.get("threads", thread_id)
            own, ctx = list(th.get("subscriptions") or []), list(th.get("context") or [])
            if mode == "own":
                if tid not in own: own.append(tid)
                ctx = [x for x in ctx if x != tid]                                    # reading became owning
            elif tid not in own and tid not in ctx: ctx.append(tid)
            self.store.update("threads", thread_id, {"subscriptions": own, "context": ctx, "updated": now()})
            self._correlate([t["thread"], thread_id])
        return {"ticket": tid, "thread": thread_id, "mode": "own" if tid in own else "context" if tid in ctx else mode}

    def _resumable(self, tid):
        t = self.store.get("tickets", tid)
        return bool(t) and t["state"] == "ended" and t.get("ended_by") not in ("person", "handoff")          # a handed-over job has one owner now: the receiver

    def _fan_out(self, tid, trigger, context=False):
        """Deliver a Trigger to a subscribed Ticket. Returns False when the Ticket could not take it (failed revalidation). `context`: read-only Thread, so the Ticket is not woken and the message is not a person's word to it."""
        t = self.store.get("tickets", tid)
        item = {"kind": "trigger", "trigger": trigger["id"], "actor": trigger["actor"], "text": trigger["text"], "human": trigger["human"] and not context, "ts": now(), "delivered": False, "trusted": self.trusted(trigger), **({"context": True} if context else {})}
        held = (not trigger["human"] and not context and t["state"] == "open" and ((t.get("last_report") or {}).get("state") in ("blocked", "failed")))
        if held: item["held"] = True                                           # dsh-superagent 003 §53: a ticket that is stuck gets no automatic input; CI and bot messages are recorded, not delivered
        inputs = (t.get("inputs") or []) + [item]
        upd = {"inputs": inputs, "updated": now()}
        if t["state"] == "ended" and trigger["human"] and not context and t.get("ended_by") not in ("person", "handoff"):
            ok, why = self.revalidate(t)
            if ok: upd.update({"state": "open", "ended_by": None, "outcome": None}); self._ack(t, f"Resuming on your message. {AGENT_LINE}")
            else: self.store.update("triggers", trigger["id"], {"disposition": f"not-resumed:{why}"}); return False
        self.store.update("tickets", tid, upd)
        if item["trusted"] and trigger["human"] and not context and CORRECTION_RE.search(trigger.get("text") or "") and (t.get("last_report") or self.store.all("assignments", "WHERE ticket=?", (tid,))):      # only the person's own words teach (a stranger's comment is data, never a rule)
            self.note_correction(t, "redirect", trigger.get("text") or "", context=(t.get("last_report") or {}).get("text") or "")      # the agent had already done something and the person set it right
        sid = self.running.get(tid); state = upd.get("state", t["state"])
        if held: return True
        if sid and state == "open":
            msg = {"kind": "context" if context else "trigger", "actor": trigger["actor"], "text": trigger["text"], "thread": trigger["threads"][0], "human": bool(trigger.get("human")) and not context}
            if not item["trusted"]: msg.update({"text": unverified(trigger["actor"], trigger["text"]), "human": False, "trusted": False})        # fenced, and it does not interrupt the model call as a person would
            try: self.cp.inbox(sid, msg); item["delivered"] = True
            except InfraError as e: self.problems["delivery"] = {"error": str(e)[:200], "since": now()}
            self.store.update("tickets", tid, {"inputs": inputs})
        elif state == "open" and trigger["human"] and not context and not any(a["state"] in ("pending", "running") for a in self.store.all("assignments", "WHERE ticket=?", (tid,))):
            self._new_assignment(tid, recovery=True)          # the sandbox is gone: a fresh Assignment restores the workspace layer
        return True

    def _return_unread(self, tid):
        """dsh-superagent 003 §39: when a Ticket ends, a person's message it never took in goes back to Eval, so it is judged afresh instead of lost."""
        t = self.store.get("tickets", tid)
        if not t or t["state"] != "ended" or t.get("ended_by") in ("person", "handoff"): return 0
        inputs, n = list(t.get("inputs") or []), 0
        for i in inputs:
            if i.get("human") and not i.get("delivered") and not i.get("context") and not i.get("returned") and i.get("trigger"):
                trig = self.store.get("triggers", i["trigger"]) or {}
                th = (trig.get("threads") or [t["thread"]])[0]; thread = self.store.get("threads", th) or {}
                ev = thread.get("eval") or {"triggers": [], "queued": now()}
                if i["trigger"] not in ev["triggers"]: ev["triggers"].append(i["trigger"])
                self.store.update("threads", th, {"eval": ev, "updated": now()}); self.store.update("triggers", i["trigger"], {"disposition": "returned-to-eval"}); i["returned"] = True; n += 1
        if n: self.store.update("tickets", tid, {"inputs": inputs})
        return n

    def revalidate(self, t):
        m = t["mandate"]
        src = self.policy.source(t["source"])
        if m["strength"] == "explicit" and m["basis"] in ("listed-addressed", "listed-followup"):
            return (True, "") if self.policy.is_allowed(t["source"], m["actor"]) else (False, "actor no longer listed")
        if str(m.get("basis", "")).startswith("table:") or m.get("basis") == "unlisted-followup":   # must still reach its strength under today's table
            synthetic = {"source": t["source"], "actor": m["actor"], "human": True, "addressed": m["basis"] != "unlisted-followup", "scope": m.get("received_as") or ""}
            strength, _ = self.policy.gate(synthetic, True)
            if strength is None or (m["strength"] == "explicit" and strength != "explicit"): return False, "admission no longer grants the original strength"
            if strength == "inferred" and self.policy.p["mode"] != "auto": return False, "mode not auto"
            return True, ""
        if m["strength"] == "inferred":
            if self.policy.p["mode"] != "auto": return False, "mode not auto"
            names = {r["name"] for r in self.policy.enabled_rules()}
            if not set(m.get("matched_rules") or []) & names: return False, "matched rules disabled"
        if m["basis"] == "listed-bot" and m["actor"] not in src.get("bots", []): return False, "bot no longer listed"
        return True, ""

    # ---- people
    def stop_ticket(self, tid, by="person", reason=""):
        t = self.store.get("tickets", tid)
        if not t: raise KeyError(tid)
        if by == "person" and t["state"] == "open": self.note_correction(t, "stopped", reason or "")
        self.store.update("tickets", tid, {"state": "ended", "ended_by": by, "outcome": "stopped", "attention": None, "updated": now()})
        sid = self.running.pop(tid, None)
        if sid: self.cp.destroy(sid)                      # a person's stop is final: the sandbox goes, the running Assignment ends with an infra error it never reports
        for a in self.store.all("assignments", "WHERE ticket=? AND state IN ('pending','running')", (tid,)):
            self.store.update("assignments", a["id"], {"state": "ended", "ended": now(), "error": "stopped by a person"})
        return {"stopped": tid}

    def retry_ticket(self, tid):
        t = self.store.get("tickets", tid)
        if not t: raise KeyError(tid)
        if t["state"] != "open": raise ValueError("ticket is not open")
        if self.running.get(tid) or any(a["state"] in ("pending", "running") for a in self.store.all("assignments", "WHERE ticket=?", (tid,))): raise ValueError("an assignment is pending or running")
        return {"assignment": self._new_assignment(tid, recovery=True)}

    def answer_ticket(self, tid, text, actor="panel", learn=True):
        t = self.store.get("tickets", tid)
        if not t: raise KeyError(tid)
        if learn and not t.get("attention") and CORRECTION_RE.search(text or "") and (t.get("last_report") or self.store.all("assignments", "WHERE ticket=?", (tid,))):
            self.note_correction(t, "redirect", text, context=(t.get("last_report") or {}).get("text") or "")       # not an answer to its question: a correction
        item = {"kind": "answer", "actor": actor, "text": text, "human": True, "ts": now(), "delivered": False}
        self.converse("message", actor=actor, text=text, ticket=tid, origin={"source": "workbench", "thread": t["thread"]})
        inputs = (t.get("inputs") or []) + [item]
        upd = {"inputs": inputs, "attention": None, "updated": now()}
        if t["state"] == "ended" and t.get("ended_by") not in ("person", "handoff"): upd.update({"state": "open", "ended_by": None, "outcome": None})
        self.store.update("tickets", tid, upd)
        sid = self.running.get(tid)
        if sid:
            try: self.cp.inbox(sid, {"kind": "answer", "actor": actor, "text": text}); item["delivered"] = True; self.store.update("tickets", tid, {"inputs": inputs})
            except InfraError: pass
            return {"delivered": True}
        if upd.get("state", t["state"]) == "open":
            aid = self._new_assignment(tid, recovery=True)
            return {"assignment": aid} if aid else {"queued": True, "note": "an assignment is already pending; the answer goes into its ticket"}
        return {"delivered": False}

    def reevaluate(self, trig_id):
        tr = self.store.get("triggers", trig_id)
        if not tr: raise KeyError(trig_id)
        th = tr["threads"][0]; thread = self.store.get("threads", th)
        ev = thread.get("eval") or {"triggers": [], "queued": now()}
        if trig_id not in ev["triggers"]: ev["triggers"].append(trig_id)
        self.store.update("threads", th, {"eval": ev}); self.store.update("triggers", trig_id, {"disposition": "queued"})
        return {"queued": trig_id}

    def set_paused(self, v):
        self.paused = bool(v); self.store.kv_set("paused", self.paused); return {"paused": self.paused}

    def converse_in(self, body):
        """POST /conversation: with a ticket it is an Input to that job; otherwise a new request through the Gate."""
        text = (body.get("text") or "").strip()
        if not text: raise ValueError("text required")
        actor = body.get("actor") or "workbench"
        if body.get("ticket"): return {"ticket": body["ticket"], **self.answer_ticket(body["ticket"], text, actor=actor)}
        t = workbench_trigger(body, actor=actor); disp = self.receive(t)
        return {"trigger": t["id"], "thread": t["threads"][0], "disposition": disp}

    def chat_hook(self, headers, body):
        """POST /hooks/chat from the chat tool (docs/002 Connector): the shared secret proves the caller; a message becomes a Trigger."""
        if not self.chat.verify(headers, body): raise PermissionError("bad chat secret")
        m = self.chat.parse(body)
        if m is None: return {"ignored": True}
        if "challenge" in m: return {"challenge": m["challenge"]}
        if not m.get("text"): return {"ignored": True}
        return {"trigger": self.chat.trigger(m)["id"], "disposition": self.receive(self.chat.trigger(m))}
