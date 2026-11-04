# Views: stage, ticket, attention, conversation, live, health (docs/008).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *

SAY_PURPOSE = {"ack": "acknowledgement", "question": "attention", "ready": "result"}       # old reply purposes, kept for the ticket view and test-intake
from clients import InfraError


class ViewsMixin:
    def store_tickets_on(self, thread_id):
        return [t["id"] for t in self.store.all("tickets", "WHERE thread=?", (thread_id,))]

    # ---- views
    def stage(self, t):
        if t["state"] == "ended": return "ended", {"ready": "已完成", "merged": "PR 已合并", "closed": "PR 已关闭", "dropped": "无需处理", "stopped": "人工停止"}.get(t.get("outcome") or "", "已结束")
        lr = t.get("last_report") or {}; running = t["id"] in self.running
        inputs = t.get("inputs") or []
        if any(i.get("human") and not i.get("delivered") for i in inputs) and not running and t["state"] == "open" and lr.get("state") in ("blocked", "failed"): return "person/delivery", "输入未送达"
        if t["state"] == "open" and self.store.all("approvals", "WHERE ticket=? AND state='pending'", (t["id"],)): return "person/approve", "等待同意"
        if t.get("attention"): return "person/answer", "等待回答"
        if lr.get("state") == "ready" and t["state"] == "open": return "person/review", "等待审阅"
        if lr.get("state") in ("blocked", "failed") and not any(i.get("human") and i.get("ts", 0) > lr.get("at", 0) for i in inputs): return "person/" + lr["state"], "受阻" if lr["state"] == "blocked" else "失败"
        if running: return "working", "工作中"
        if t["state"] == "open" and not lr: return "working/queued", "排队"
        if lr.get("state") == "waiting" or (t["state"] == "open" and not running): return "idle", "等待 Trigger"
        return "working", "工作中"

    def ticket_view(self, tid, full=True):
        t = self.store.get("tickets", tid)
        if not t: return None
        stage, label = self.stage(t)
        v = {k: t[k] for k in ("id", "short", "title", "thread", "source", "state", "ended_by", "outcome", "branch", "pr", "attention", "last_report", "workspace_layer", "created", "updated")}
        v.update({"stage": stage, "stage_label": label, "running": t["id"] in self.running, "strength": t["mandate"]["strength"], "actor": t["mandate"]["actor"]})
        if full:
            v["mandate"] = t["mandate"]; v["inputs"] = t.get("inputs") or []; v["channels"] = t.get("channels") or []
            v["assignments"] = [{k: a[k] for k in ("id", "state", "sandbox", "node", "layers", "agent_exit", "cost", "tokens_in", "tokens_out", "tokens_cached", "elapsed_s", "error", "recovery", "created", "started", "ended")} | {"home": a.get("home") or "", "reports": a.get("reports") or [], "has_traj": bool(a.get("traj"))}
                                for a in self.store.all("assignments", "WHERE ticket=?", (tid,))]
            v["replies"] = [{"purpose": SAY_PURPOSE.get(m.get("say"), m.get("say")), "say": m.get("say"), "text": m["text"], "state": "sent" if m.get("delivered") else "queued", "created": m["created"]}
                            for m in self.store.all("conversation", "WHERE ticket=? AND kind='say'", (tid,))]   # what the agent said about this ticket, whatever channel it went to
            subs = self.store.get("threads", t["thread"]) or {}
            v["subscriptions"] = subs.get("subscriptions") or []
            mine = [th for th in self.store.all("threads") if t["id"] in (th.get("subscriptions") or []) or t["id"] in (th.get("context") or [])]
            v["follows"] = [{"thread": th["id"], "mode": "own" if t["id"] in (th.get("subscriptions") or []) else "context"} for th in mine]      # every Thread this Ticket follows, and how
            v["correlated"] = subs.get("correlated") or []
        return v

    def assignment_view(self, aid):
        a = self.store.get("assignments", aid)
        return a

    def live(self, tid):
        """The running assignment's trajectory so far (the harness writes it every step) plus the newest kernel events."""
        t = self.store.get("tickets", tid)
        if not t: raise KeyError(tid)
        sid = self.running.get(tid)
        out = {"ticket": tid, "running": bool(sid), "sandbox": sid, "messages": [], "events": []}
        if not sid: return out
        try:
            raw = self.cp.get(sid, "/tmp/aios/traj.json"); traj = json.loads(raw)
            out["messages"] = traj.get("messages", [])[-200:]; out["exit_status"] = (traj.get("info") or {}).get("exit_status")
            out["calls"] = ((traj.get("info") or {}).get("model_stats") or {}).get("api_calls"); out["cost"] = ((traj.get("info") or {}).get("model_stats") or {}).get("instance_cost")
        except (InfraError, ValueError): pass
        out["events"] = self.cp.events(sid)[-100:]
        return out

    def conversation(self, tid):
        """The ticket as one conversation: inputs, reports, replies and the saved trajectories of ended assignments, in time order."""
        t = self.store.get("tickets", tid)
        if not t: raise KeyError(tid)
        items = []
        for i in t.get("inputs") or []: items.append({"ts": i.get("ts", 0), "kind": "input", "actor": i.get("actor"), "text": i.get("text"), "input_kind": i.get("kind")})
        for a in self.store.all("assignments", "WHERE ticket=?", (tid,)):
            items.append({"ts": a.get("started") or a.get("created") or 0, "kind": "assignment", "id": a["id"], "state": a["state"], "sandbox": a.get("sandbox"), "recovery": a.get("recovery"), "error": a.get("error"),
                          "cost": a.get("cost"), "tokens_in": a.get("tokens_in"), "tokens_out": a.get("tokens_out"), "tokens_cached": a.get("tokens_cached"), "elapsed_s": a.get("elapsed_s"), "traj": a.get("traj")})
            for r in a.get("reports") or []: items.append({"ts": (a.get("ended") or a.get("started") or 0) - 0.001, "kind": "report", "assignment": a["id"], **r})
        for m in self.store.all("conversation", "WHERE ticket=? AND kind='say'", (tid,)):            # what the agent said about this job (docs/006), whether or not a channel carried it
            items.append({"ts": m["ts"], "kind": "reply", "purpose": m.get("say"), "say": m.get("say"), "text": m["text"], "state": "sent" if m.get("delivered") else "queued"})
        items.sort(key=lambda x: x["ts"])
        return {"ticket": self.ticket_view(tid, full=False), "items": items}

    def thread_view(self, th):
        t = self.store.get("threads", th)
        if not t: return None
        t["triggers"] = sorted([x for x in self.store.all("triggers") if th in (x.get("threads") or [])], key=lambda x: -x["created"])
        t["decisions"] = self.store.all("decisions", "WHERE thread=?", (th,), order="created DESC")
        t["tickets"] = [self.ticket_view(x, full=False) for x in self.store_tickets_on(th)]
        return t

    def attention(self):
        tickets = [self.ticket_view(t["id"], full=False) for t in self.store.all("tickets", "WHERE state='open'")]
        need = [t for t in tickets if t["stage"].startswith("person/")]
        order = {"person/delivery": 0, "person/approve": 1, "person/answer": 1, "person/review": 2, "person/blocked": 3, "person/failed": 3}
        need.sort(key=lambda t: (order.get(t["stage"], 9), -(t["updated"] or 0)))
        unavailable = [d for d in self.store.all("decisions", "WHERE outcome='unavailable'", order="created DESC") if now() - d["created"] < 86400][:10]
        day = now() - 86400
        counts = {"triggers_24h": sum(1 for x in self.store.all("triggers") if x["created"] > day), "decisions_24h": sum(1 for x in self.store.all("decisions") if x["created"] > day),
                  "tickets_open": len(tickets), "running": len(self.running), "slots": self.cfg["max_parallel"], "would_accept_24h": sum(1 for x in self.store.all("decisions") if x["created"] > day and x.get("would_accept"))}
        problems = [{"name": k, **v} for k, v in self.problems.items() if now() - v["since"] < 3600 or k.startswith("watchdog:")]
        return {"needs_person": need, "unavailable": unavailable, "problems": problems, "counts": counts, "paused": self.paused, "policy_version": self.policy.version, "mode": self.policy.p["mode"],
                "sources": self.sources_view()}

    def sources_view(self):
        k = getattr(self.gitea, "kind", "gitea")
        return {"workbench": {"enabled": True}, k: {"enabled": self.gitea.enabled, "url": self.gitea.url, "repo": self.gitea.repo, "bot": self.gitea.bot,
                                                   "cursor": self.store.kv_get(f"{k}.cursor"), "error": (self.problems.get(k) or {}).get("error")}}

    def connectors_view(self):
        """docs/001 第 2 阶段: where the agent is connected. The workbench always; the code host as configured."""
        k = getattr(self.gitea, "kind", "gitea")
        return {"connectors": [{"kind": "workbench", "title": "工作台", "enabled": True},
                               {"kind": k, "title": {"gitea": "Gitea", "github": "GitHub"}.get(k, k), "url": self.gitea.url, "repo": self.gitea.repo, "bot": self.gitea.bot, "enabled": self.gitea.enabled,
                                "readable": self.gitea.readable, "paused": bool(self.store.kv_get(f"connector.{k}.paused", False)), "cursor": self.store.kv_get(f"{k}.cursor"),
                                "error": (self.problems.get(k) or {}).get("error")},
                               {"kind": "chat", "title": {"feishu": "飞书", "slack": "Slack"}.get(self.chat.format, "聊天工具"), "url": self.chat.url, "bot": self.chat.bot, "enabled": self.chat.enabled, "readable": self.chat.readable,
                                "paused": False, "cursor": None, "error": (self.problems.get("chat") or {}).get("error")}]}

    def connector_pause(self, kind, paused):
        if kind != getattr(self.gitea, "kind", "gitea"): raise KeyError(kind)
        self.store.kv_set(f"connector.{kind}.paused", bool(paused)); return self.connectors_view()

    def spent_today(self):
        """What the agent's jobs have cost since local midnight (model calls made inside the sandboxes; the host's own small calls are not counted)."""
        lt = time.localtime(); start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        done = sum(float(a.get("cost") or 0) for a in self.store.all("assignments", "WHERE created>=?", (start,)))
        live = 0.0
        for sid in list(self.running.values()):                                           # what the running ones have spent so far counts too: several started together must not slip under the limit
            try: live += float((self.proxy.usage(sid) if self.proxy else {}).get("cost_usd") or 0)
            except Exception: pass  # noqa: BLE001
        return done + live

    def over_daily_budget(self):
        return self.billing_blocked() or self.over_cap()

    def billing_blocked(self):
        b = self.problems.get("billing")
        return bool(b) and now() - b["since"] < 3600                                       # the provider refused for balance within the hour: what it would start on its own would only fail

    def over_cap(self):
        try: cap = float((self.policy.p.get("defaults") or {}).get("daily_budget_usd") or 0)
        except (TypeError, ValueError): cap = 0.0
        return cap > 0 and self.spent_today() >= cap

    def health(self):
        try: cp_ok = bool(self.cp._req("GET", "/health", timeout=10).get("ok"))
        except InfraError: cp_ok = False
        return {"ok": True, "cp": cp_ok, "gitea": self.gitea.enabled, "model": bool(self.proxy), "tickets": len(self.store.all("tickets")), "running": len(self.running),
                "policy_version": self.policy.version, "mode": self.policy.p["mode"], "paused": self.paused, "layers": self.cfg["layers"],
                "spent_today": round(self.spent_today(), 4), "daily_budget_usd": (self.policy.p.get("defaults") or {}).get("daily_budget_usd")}
