# Handing work between a user's agents (docs/002 Handoff, D17): the seams are written down before the feature. One owner at a time: a hand-off
# ends the giver's ticket and opens one for the receiver, which carries who gave it, the chain so far and whether anything on the way came from
# a source the person had not vouched for. Only the owner may draft an irreversible action; the giver's ticket is closed to them. The receiver's
# results come home: mirrored as notes into the giver's conversation, and sent back to the channel the request came from.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *
from sources import workbench_trigger

MAX_CHAIN = 3                  # hand-offs in a row before the work goes to the person instead
MIRRORED = ("ready", "stuck", "question", "approval")


class SelfHandoff(ValueError):
    """The agent asked to hand the work to itself: usually the text it was given says "the colleague X does this" and X is the reader. Not an error to show the person."""


class HandoffMixin:
    engines = None             # set by Engines: the sibling agents of this user

    def handoff_ticket(self, t, to, brief):
        """The giver's ticket moves to agent `to`. Raises ValueError (said to the person as 'stuck') when it cannot: no such agent, itself, a loop, too deep."""
        eng = self.engines
        owner = self.cfg.get("owner") or self.user; me = self.cfg.get("agent") or "main"
        to = (to or "").strip().lstrip("@")
        if eng is None: raise ValueError("this deployment has no other agents")
        if to == me: raise SelfHandoff("an agent cannot hand work to itself")
        if not any(a["name"] == to for a in eng.agents(owner)): raise ValueError(f"no agent named {to}")
        prior = (t["mandate"].get("handoff") or {}).get("chain") or []
        chain = list(prior) + [me]
        if to in chain: raise ValueError(f"loop: {' -> '.join(chain + [to])}")
        if len(chain) > MAX_CHAIN: raise ValueError(f"more than {MAX_CHAIN} hand-offs in a row")
        brief = (brief or "").strip()[:2000] or t["title"]
        orig = next((i.get("text") for i in (t.get("inputs") or []) if i.get("kind") == "trigger"), "") or t["title"]
        tainted = any(i.get("trusted") is False for i in (t.get("inputs") or []))
        target = eng.get(owner, to)
        text = f"@{me} handed this to you (job {t['short']}).\nBrief from @{me}:\n{brief}\n\nWhat the person originally asked:\n{orig[:1500]}"
        trig = workbench_trigger({"text": text, "conversation": f"handoff-{t['short']}-{to}", "actor": f"agent:{me}"})
        origin_ch = next((c for c in (t.get("channels") or []) if c.get("kind") != "workbench"), None)
        if origin_ch: trig["channel"] = origin_ch                                   # the answer goes back to where the person asked
        trig["meta"].update({"title": f"@{me} → {brief[:70]}", "tainted": tainted, "handoff": {"from_agent": me, "from_ticket": t["id"], "from_short": t["short"], "owner": owner, "chain": chain}})
        threading.Thread(target=target.receive, args=(trig,), daemon=True).start()       # not under the giver's lock: two agents handing to each other must not wait on each other
        mandate = dict(t["mandate"]); mandate["handoff_out"] = {"to": to, "trigger": trig["id"], "at": now()}
        self.store.update("tickets", t["id"], {"mandate": mandate, "attention": None, "updated": now()})        # not ended yet: the work is the giver's until the receiver has taken it (handoff_accepted)
        self.converse("note", actor="system", text=f"交给 @{to} 了:{brief[:140]}。它做完会在这里和你问的那个地方告诉你。", ticket=t["id"], meta={"handoff": {"to": to, "chain": chain, "trigger": trig["id"]}})
        return {"to": to, "trigger": trig["id"], "disposition": "sent"}

    # ---- the receiver's answer comes back to the giver (called from the receiver's engine, in a thread: the two locks never nest)
    def handoff_accepted(self, from_ticket, to_agent, to_ticket):
        with self.lock:
            t = self.store.get("tickets", from_ticket)
            if not t or t["state"] != "open" or not (t["mandate"].get("handoff_out") or {}): return
            self.store.update("tickets", from_ticket, {"state": "ended", "ended_by": "handoff", "outcome": "handed-off", "attention": None, "updated": now()})

    def handoff_refused(self, from_ticket, to_agent, reason):
        """The receiver's gate or policy did not take the job: it stays the giver's, and the person is told instead of the work vanishing."""
        with self.lock:
            t = self.store.get("tickets", from_ticket)
            if not t or t["state"] != "open" or not (t["mandate"].get("handoff_out") or {}): return
            mandate = dict(t["mandate"]); mandate.pop("handoff_out", None)
            q = f"想交给 @{to_agent},但它没有接下({str(reason)[:120]})。要我自己做吗?回复「做」,或补一句说明。"
            self.store.update("tickets", from_ticket, {"mandate": mandate, "attention": {"question": q, "asked": now(), "assignment": None}, "updated": now()})
            self.say(t, "stuck", f"Could not hand this to @{to_agent}: {str(reason)[:160]}. Reply to have me do it myself. {AGENT_LINE}")

    def handoff_check(self):
        """A hand-off nobody answered within ten minutes is a refused one."""
        for t in self.store.all("tickets", "WHERE state='open'"):
            h = (t.get("mandate") or {}).get("handoff_out")
            if h and now() - h.get("at", now()) > 600: self.handoff_refused(t["id"], h.get("to"), "no answer in ten minutes")

    def _handoff_signal(self, trigger, how, arg):
        h = (trigger.get("meta") or {}).get("handoff")
        if not h or self.engines is None: return
        me = self.cfg.get("agent") or "main"
        def run():
            try:
                giver = self.engines.get(h["owner"], h["from_agent"])
                if how == "accepted": giver.handoff_accepted(h["from_ticket"], me, arg)
                else: giver.handoff_refused(h["from_ticket"], me, arg)
            except Exception as e:  # noqa: BLE001
                self.problems["handoff"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}
        threading.Thread(target=run, daemon=True).start()

    def mirror_handoff(self, ticket, kind, text):
        """What the receiver says about a handed-over ticket is also a note in the giver's conversation: one place to read the whole story."""
        h = (ticket.get("mandate") or {}).get("handoff")
        if not h or kind not in MIRRORED or self.engines is None: return
        try:
            giver = self.engines.get(h["owner"], h["from_agent"])
            giver.converse("note", actor=f"agent:{self.cfg.get('agent') or 'main'}", text=f"@{self.cfg.get('agent') or 'main'}:{text}"[:1200], ticket=h["from_ticket"], meta={"handoff": h, "say": kind})
        except Exception as e:  # noqa: BLE001 - the mirror is a convenience; the receiver's own conversation has the say
            self.problems["handoff"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}
