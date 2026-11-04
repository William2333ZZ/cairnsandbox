# The conversation and say, outbox, approvals and their execution, PR follow (docs/005 批准, docs/006 对用户说话的规矩).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError


class RelayMixin:
    # ---- approvals (docs/005 批准, docs/009): the draft of an irreversible action; nothing happens until a person says yes
    def _approval(self, ticket, kind, action, draft):
        tk = self.store.get("tickets", ticket["id"]) if ticket.get("id") else None
        if tk and (tk.get("ended_by") == "handoff" or (tk.get("mandate") or {}).get("handoff_out")): return None      # one owner at a time: a ticket handed over (or being handed over) can no longer draft irreversible actions
        pend = [a for a in self.store.all("approvals", "WHERE ticket=? AND state='pending' AND kind=?", (ticket["id"], kind))] if ticket.get("id") else []
        if pend:                                                                   # the same draft again (a PR that moved): keep one pending row, refresh it -- and look at it again, a moved PR is a different thing to approve
            res = self.review_action(ticket, kind, action or {}, draft); review = {k: v for k, v in res.items() if not k.startswith("_")}
            self.store.update("approvals", pend[0]["id"], {"action": action, "draft": draft, "review": review})
            if ticket.get("id") and kind in ("merge", "send"): threading.Thread(target=self.review_with_model, args=(pend[0]["id"], ticket, kind, draft, res), daemon=True).start()      # a moved PR is asked about again
            return pend[0]["id"]
        aid = new_id("ap")
        res = self.review_action(ticket, kind, action or {}, draft)                  # the host's own look, with rules the agent cannot edit (docs/009 独立审查)
        review = {k: v for k, v in res.items() if not k.startswith("_")}
        self.store.insert("approvals", {"id": aid, "ticket": ticket["id"], "kind": kind, "action": action or {}, "draft": draft, "state": "pending", "actor": None, "created": now(), "decided": None, "decided_by": None, "result": None, "error": None, "review": review})
        flags = "".join(f"\n[{'拦下' if r['level'] == 'block' else '注意'}] {r['why']}" for r in review["reasons"])
        self.say(ticket, "approval", f"要你同意:{draft}{flags} {AGENT_LINE}", meta={"approval": aid, "kind": kind, "review": review["verdict"]})
        if ticket.get("id") and kind in ("merge", "send"): threading.Thread(target=self.review_with_model, args=(aid, ticket, kind, draft, res), daemon=True).start()
        if ticket.get("id"): self.store.update("tickets", ticket["id"], {"updated": now()})
        return aid

    def _approval_action(self, t, kind, act, draft):
        """The harness drafts `APPROVE: send|budget|routine` plus text; the structured action the relay needs (where, how much, which schedule) is worked out here, on the host."""
        src = act if isinstance(act, dict) else {}; act = {"kind": kind}                # nothing the harness put in the action survives unless the draft shows it: what the person reads is what is done
        if kind == "send":
            ch = next((c for c in (t.get("channels") or []) if c.get("kind") != "workbench"), None)
            said = (src or {}).get("text"); act["text"] = said if isinstance(said, str) and said.strip() and said.strip() in (draft or "") else draft           # the words sent are the words shown: its own text only when the draft contains it
            to = {k: ch[k] for k in ("kind", "issue", "thread") if k in ch} if ch else {}
            want = ((src or {}).get("to") or {}) if isinstance((src or {}).get("to"), dict) else {}
            if want.get("issue") and re.search(r"(?:#|issue\s*|pr\s*)" + re.escape(str(want["issue"])) + r"\b", draft or "", re.I) and want.get("kind") in ("gitea", "github"): to = {"kind": want["kind"], "issue": want["issue"]}      # another place only if the draft names it
            act["to"] = to
        elif kind == "budget":
            m = re.search(r"\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:美元|刀|usd|dollars?)", draft or "", re.I)
            if m: act["amount_usd"] = float(m.group(1) or m.group(2))
        elif kind == "routine":
            sc = parse_routine(draft or "")
            if sc: act["schedule"] = sc
        return act

    def approvals(self, state=None):
        rows = self.store.all("approvals", "WHERE state=?" if state else "", (state,) if state else (), order="created DESC")
        for a in rows:
            t = self.store.get("tickets", a["ticket"]); a["ticket_title"] = (t or {}).get("title"); a["short"] = (t or {}).get("short")
        return rows

    def decide_approval(self, aid, decision, actor="panel", reason="", override=False):
        with self.lock:                                                             # claim it: two clicks (or two tabs) must not run the action twice
            a = self.store.get("approvals", aid)
            if not a: raise KeyError(aid)
            if a["state"] != "pending": raise ValueError(f"approval is {a['state']}")
            if decision not in ("approve", "deny"): raise ValueError("decision must be approve or deny")
            rv = a.get("review") or {}
            if decision == "approve" and rv.get("verdict") == "block" and not override:
                raise ValueError("审查拦下了这个动作:" + "; ".join(r["why"] for r in rv.get("reasons", []) if r["level"] == "block")[:300] + "。确要同意,请再确认一次(override)")
            self.store.update("approvals", aid, {"state": "deciding"})
        t = self.store.get("tickets", a["ticket"]) if a.get("ticket") else None
        upd = {"state": "approved" if decision == "approve" else "denied", "decided": now(), "decided_by": actor}
        result = None
        if decision == "approve":
            try: result = self._relay(a["kind"], a.get("action") or {}, t)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                self.store.update("approvals", aid, {"state": "pending", "error": err})   # nothing happened: it stays a draft the person can look at and try again
                return {**self.store.get("approvals", aid)}
            upd["result"] = result; upd["error"] = None
        self.store.update("approvals", aid, upd)
        if decision == "deny" and t: self.note_correction(t, "denied", reason or "", context=f"{a['kind']}: {a.get('draft') or ''}")       # a denial is the clearest correction there is
        with self.lock:
            if a["kind"] != "merge" and t and t["state"] != "ended":                  # the agent goes on with the answer; a merge ends the job through the PR instead
                self.answer_ticket(t["id"], f"[{a['kind']}] {'approved' if decision == 'approve' else 'denied'} by {actor}" + (f": {reason}" if reason and decision == "deny" else "") + (f": {json.dumps(result, ensure_ascii=False)[:300]}" if result else ""), actor=actor, learn=False)
            elif a["kind"] == "merge" and decision == "approve" and t:
                pr = dict(t.get("pr") or {}); pr["state"] = "merged"
                self.store.update("tickets", t["id"], {"pr": pr, "state": "ended", "ended_by": "pr-merged", "outcome": "merged", "updated": now()})
                self.say(t, "ready", f"Merged PR #{pr.get('number')}. {AGENT_LINE}")
                self._mine(self.store.get("tickets", t["id"]))
        if t: self.store.update("tickets", a["ticket"], {"updated": now()})
        return {**self.store.get("approvals", aid)}

    def _relay(self, kind, action, t):
        """Execute an approved action on the host side with the host's credentials (never from a sandbox)."""
        if kind == "merge":
            if not self.gitea.enabled: raise RuntimeError("no code host configured")
            n = action.get("pr") or (t.get("pr") or {}).get("number")
            if not n: raise RuntimeError("no pull request to merge")
            self.gitea.merge_pr(n, head=action.get("head")); return {"merged": n}               # pinned to the head that was reviewed: if the PR moved since, the host refuses and the draft is looked at again
        if kind == "send":
            to = action.get("to") or {}
            if to.get("kind") in ("gitea", "github") and to.get("issue") and self.gitea.enabled: self.gitea.comment(to["issue"], (action.get("text") or "")[:4000]); return {"sent": to}
            if to.get("kind") == "chat" and self.chat.enabled: self.chat.send((action.get("text") or "")[:4000], thread=to.get("thread")); return {"sent": to}
            raise RuntimeError("send target not supported")
        if kind == "budget":
            extra = float(action.get("amount_usd") or 0)
            if extra <= 0 or not t: raise RuntimeError("no amount")
            m = dict(t.get("mandate") or {}); m["budget_usd"] = float(m.get("budget_usd") or 0) + extra
            self.store.update("tickets", t["id"], {"mandate": m}); return {"budget_usd": m["budget_usd"]}
        if kind == "routine":
            sc = dict(action.get("schedule") or {}); sc.setdefault("id", new_id("rt")); sc.setdefault("enabled", True)
            if not sc.get("text") or not (sc.get("every_s") or sc.get("daily_at")): raise RuntimeError("routine needs text and every_s or daily_at")
            self.policy.update({"schedules": [x for x in self.policy.p.get("schedules") or [] if x.get("id") != sc["id"]] + [sc]}, origin="approval"); return {"routine": sc["id"]}
        raise RuntimeError(f"unknown kind {kind}")

    def _mine(self, t):
        try: self.mine_after_merge(t)
        except Exception as e: self.problems["mining"] = {"error": f"{type(e).__name__}: {e}"[:300], "since": now(), "ticket": t["id"]}  # noqa: BLE001

    def expire_approvals(self):
        for a in self.store.all("approvals", "WHERE state='pending'"):
            if not a.get("ticket"): continue                                             # a routine draft waits for the person, however long
            t = self.store.get("tickets", a["ticket"])
            if not t or t["state"] == "ended": self.store.update("approvals", a["id"], {"state": "expired", "decided": now()})

    # ---- the conversation (docs/005 对话与路由, docs/006 对用户说话的规矩)
    def converse(self, kind, actor=None, text="", ticket=None, origin=None, dest=None, say=None, meta=None, delivered=0):
        row = {"id": new_id("v"), "ts": now(), "kind": kind, "say": say, "actor": actor, "text": text, "ticket": ticket, "origin": origin or {}, "dest": dest or {}, "delivered": int(delivered), "meta": meta or {}, "created": now()}
        self.store.insert("conversation", row); return row

    def say(self, ticket, kind, text, meta=None):
        """The only way the agent speaks to the user: one conversation row, and for the kinds that go back to where the message came from, one outbox row per channel."""
        assert kind in SAY_KINDS, kind
        chans = [c for c in (ticket.get("channels") or []) if c.get("kind") != "workbench"] if kind in SAY_DELIVER else []
        self.converse("say", actor="agent", text=text, ticket=ticket["id"], say=kind, dest={"channels": chans}, delivered=0 if chans else 1, meta=meta)
        self.mirror_handoff(ticket, kind, text)
        for ch in chans:
            self.store.insert("outbox", {"id": new_id("m"), "channel": ch, "ticket": ticket["id"], "purpose": kind, "text": text, "state": "queued", "attempts": 0, "error": None, "created": now(), "sent": None})

    def reply(self, ticket, purpose, text):                                             # old name; purposes map onto say kinds
        self.say(ticket, {"acknowledgement": "ack", "attention": "question", "result": "ready"}.get(purpose, "ready"), text)

    def conversation_feed(self, since=0.0, limit=200):
        """The user's conversation with their agent: messages, says, work markers, notes; newest last. Rows with a ticket carry its stage."""
        rows = self.store.all("conversation", "WHERE ts>?", (float(since or 0),), order="ts")[-int(limit):]
        for r in rows:
            if r.get("ticket"):
                t = self.store.get("tickets", r["ticket"])
                if t: r["stage"] = self.ticket_view(t["id"], full=False)["stage"]; r["pr"] = t.get("pr"); r["short"] = t.get("short")
        return {"items": rows, "now": now()}

    def digests(self):
        """001 第 5 阶段: a job still running after a day gets one line a day in the conversation, nowhere else."""
        for t in self.store.all("tickets", "WHERE state='open'"):
            if not self.running.get(t["id"]): continue
            last = self.store.all("conversation", "WHERE ticket=? AND kind='say'", (t["id"],), order="ts")
            if now() - (last[-1]["ts"] if last else t["created"]) < 86400: continue
            a = self.store.all("assignments", "WHERE ticket=?", (t["id"],))
            self.say(t, "digest", f"Still on `{t['short']}`: {len(a)} assignment(s), ${sum((x.get('cost') or 0) for x in a):.2f} so far; last report: {(t.get('last_report') or {}).get('state', 'none')}.")

    def flush_outbox(self):
        waiting = set()                                                            # dsh-superagent 003 §43: one conversation's messages go out in the order they were queued; one that waits or fails holds the ones behind it
        for m in self.store.all("outbox", "WHERE state='queued'"):
            ch = m["channel"]; backoff = min(300, 5 * 2 ** max(0, m["attempts"] - 1)); key = json.dumps(ch, sort_keys=True)
            if key in waiting: continue
            if m["attempts"] and now() - (m.get("sent") or m["created"]) < backoff: waiting.add(key); continue
            try:
                if ch.get("kind") in ("gitea", "github") and self.gitea.enabled: self.gitea.comment(ch["issue"], m["text"])
                elif ch.get("kind") == "chat" and self.chat.enabled: self.chat.send(m["text"], thread=ch.get("thread"))
                elif ch.get("kind") == "workbench": pass                          # the panel shows the ticket's replies; nothing to send
                else: raise RuntimeError(f"no channel for {ch}")
                self.store.update("outbox", m["id"], {"state": "sent", "sent": now(), "attempts": m["attempts"] + 1})
            except urllib.error.HTTPError as e:
                gone = e.code in (404, 410)
                if not gone: waiting.add(key)
                self.store.update("outbox", m["id"], {"state": "dropped" if gone else "queued", "attempts": m["attempts"] + 1, "error": f"{e.code}", "sent": now()})
            except Exception as e:  # noqa: BLE001
                waiting.add(key)
                self.store.update("outbox", m["id"], {"state": "queued", "attempts": m["attempts"] + 1, "error": str(e)[:200], "sent": now()})
                if m["attempts"] + 1 >= 3: self.problems["outbox"] = {"error": str(e)[:200], "since": now()}

    # ---- pull request follow (ticket end)
    def follow_prs(self):
        if not self.gitea.enabled: return
        for t in self.store.all("tickets", "WHERE state='open' AND pr IS NOT NULL AND pr != 'null'"):
            pr = t.get("pr") or {}
            if not pr.get("number"): continue
            try: p = self.gitea.pull(pr["number"])
            except Exception as e:  # noqa: BLE001
                self.problems[getattr(self.gitea, "kind", "gitea")] = {"error": str(e)[:200], "since": now()}; continue
            st = p.get("state"); merged = bool(p.get("merged"))
            if st == "closed" or merged:
                pr["state"] = "merged" if merged else "closed"
                self.store.update("tickets", t["id"], {"pr": pr, "state": "ended", "ended_by": "pr-" + pr["state"], "outcome": pr["state"], "updated": now()})
                self._return_unread(t["id"])
                if merged: self._mine(self.store.get("tickets", t["id"]))
                else: self.note_correction(t, "returned", "", context=f"PR #{pr.get('number')} closed without merging: {t['title'][:120]}")
