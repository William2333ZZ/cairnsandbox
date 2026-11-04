# Routines, the agent's version, updates and rollback (docs/005 例行, docs/007 对用户的更新与回退).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError


class AgentStateMixin:
    # ---- the agent's version: updates from the version loop (docs/007 对用户的更新与回退), pin / rollback by the person
    def agent_version(self): return self.store.kv_get("agent.version", None)

    def agent_view(self):
        return {"version": self.agent_version(), "pinned": bool(self.store.kv_get("agent.pinned", False)), "previous": self.store.kv_get("agent.previous", None),
                "updates": (self.store.kv_get("agent.updates", []) or [])[-20:], "ask": self.ask_view()["total"]}

    def update(self, body):
        """POST /updates from the evolve driver: a promotion. Agents following stable switch to it and hear one update; a pinned agent only hears it."""
        vid = body.get("version"); layers = body.get("layers") or []
        if not vid or not layers: raise ValueError("version and layers required")
        st = body.get("stats") or {}
        ver = {"id": vid, "label": body.get("label") or vid, "layers": layers, "parent": body.get("parent"), "at": now()}
        ups = (self.store.kv_get("agent.updates", []) or []) + [{**ver, "changes": (body.get("changes") or "")[:800], "why": (body.get("why") or "")[:400], "stats": st, "taskset": body.get("taskset"),
                                                                 "attribution": body.get("attribution"), "kinds": body.get("kinds"), "ask": body.get("ask")}]
        self.store.kv_set("agent.updates", ups[-50:])
        pinned = bool(self.store.kv_get("agent.pinned", False)); cur = self.agent_version()
        if not pinned:
            if cur and cur.get("id") != vid: self.store.kv_set("agent.previous", cur)               # the same version announced twice (a re-judged iteration) must not make "previous" the current one
            self.store.kv_set("agent.version", ver)
        def f(x): return f"{x:.2f}" if isinstance(x, (int, float)) else "?"
        prev = next((u for u in reversed(ups[:-1]) if cur and u.get("id") == cur.get("id")), None) if cur else None
        why = (body.get("why") or "").strip()
        kinds = body.get("kinds") or {}; kc, kp = (kinds.get("cand") or {}), (kinds.get("parent") or {})
        names = {"plain": "普通题", "dependent": "依赖题(你纠正过的)", "obstacle": "要问才知道的题", "long": "做得久的题"}
        ksent = "按类看:" + "、".join(f"{names.get(k, k)} {f(kp[k]['mean'])} → {f(kc[k]['mean'])}" for k in kc if k in kp and kc[k] and kp[k]) + "。" if kc and kp and any(k in kp for k in kc) else ""
        ask = body.get("ask") or {}; ac, ap = (ask.get("cand") or {}), (ask.get("parent") or {})
        asent = f"该问就问的 F1 {f(ap.get('f1'))} → {f(ac.get('f1'))}。" if ac.get("f1") is not None and ap.get("f1") is not None else ""
        at = body.get("attribution") or {}
        tsent = (f"我预计修好 {at.get('predicted_fixes', 0)} 道,实际修好 {at.get('fixed', 0)} 道;{at.get('broke', 0)} 道变差" + (f"(其中 {at.get('lost_stable')} 道原来稳定做对)" if at.get("lost_stable") else "") + "。") if at else ""
        text = (f"我的运行系统更新了:{(body.get('changes') or '').strip()[:300] or ver['label']}。" + (f"\n原因:{why[:200]}。" if why else "") + ("\n" if st.get("mean") is not None else "") + (f"在 {body.get('taskset') or '任务集'} 上的均值 {f(st.get('parent_mean'))} → {f(st.get('mean'))}" + (f"(95% 区间 {f((st.get('ci') or [None, None])[0])} ~ {f((st.get('ci') or [None, None])[1])})" if st.get("ci") else "") + ";" if st.get("mean") is not None else "") +
                (f"每次运行花费 ${f(st.get('parent_cost'))} → ${f(st.get('cost'))}。" if st.get("cost") is not None else "") + ("\n" + ksent if ksent else "") + ("\n" + asent if asent else "") + ("\n" + tsent if tsent else "") + "\n" +
                ("(你钉住了版本,这次没有换上;回复「退回」或在版本页解除。)" if pinned else "回复「退回」可换回上一版" + (f"({(prev or {}).get('label') or (cur or {}).get('label')}:{((prev or {}).get('changes') or '').strip()[:60]})" if cur else "") + "。"))
        pseudo = {"id": None, "short": "update", "title": ver["label"], "channels": []}
        self.say(pseudo, "update", text, meta={"update": vid, "previous": (cur or {}).get("id") if cur else None, "applied": not pinned})
        return {"applied": not pinned, "version": self.agent_version(), "pinned": pinned}

    def set_agent_version(self, version, pin=None):
        """version: a {id, label, layers} dict, "previous", or None (back to the deployment default); pin: keep it through later updates."""
        cur = self.agent_version()
        if version == "previous":
            prev = self.store.kv_get("agent.previous", None)
            if not prev: return {"version": cur, "pinned": bool(self.store.kv_get("agent.pinned", False)), "changed": False}
            self.store.kv_set("agent.previous", cur); self.store.kv_set("agent.version", prev)
        elif version is None: self.store.kv_set("agent.previous", cur); self.store.kv_set("agent.version", None)
        elif isinstance(version, dict) and version.get("id") and version.get("layers"): self.store.kv_set("agent.previous", cur); self.store.kv_set("agent.version", version)
        else: raise ValueError("version must be {id, layers}, \"previous\" or null")
        if pin is not None: self.store.kv_set("agent.pinned", bool(pin))
        return {"version": self.agent_version(), "pinned": bool(self.store.kv_get("agent.pinned", False)), "changed": True}

    def routines(self):
        last = self.store.kv_get("schedule.last", {}) or {}
        return [{**sc, "last": last.get(sc.get("id"))} for sc in self.policy.p.get("schedules") or []]

    def routine_update(self, rid, body):
        scs = list(self.policy.p.get("schedules") or [])
        if not any(x.get("id") == rid for x in scs): raise KeyError(rid)
        if body.get("delete"): scs = [x for x in scs if x.get("id") != rid]
        else: scs = [{**x, **{k: body[k] for k in ("enabled", "text", "every_s", "daily_at", "budget_usd") if k in body}} if x.get("id") == rid else x for x in scs]
        self.policy.update({"schedules": scs}, origin="person"); return {"routines": self.routines()}
