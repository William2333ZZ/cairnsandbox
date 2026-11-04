# Default policy and the Policy object: Gate rules, admission table, schedules, content-hash versions (docs/005).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *

# ----------------------------------------------------------------------------------------------- policy

DEFAULT_POLICY = {
    "mode": "observe",                       # off | observe | auto
    "rules": [],                             # [{"name", "text", "enabled"}]
    "defaults": {"budget_usd": 1.0, "daily_budget_usd": 5.0, "network": "allowlist", "version": None, "scope": "the request, within this repository"},       # daily: what the agent may spend in one day on its own initiative (001 第 1 阶段)
    "schedules": [],                        # [{"id","text","every_s"|"daily_at":"HH:MM","enabled","budget_usd"}]
    "sources": {
        "workbench": {"allowed": ["*"], "bots": []},
        "ci": {"allowed": ["system"], "bots": []},               # docs/005 自动 Source: the platform files its own tickets
        "evaluation": {"allowed": ["system"], "bots": []},
        "schedule": {"allowed": ["system"], "bots": []},
        "gitea": {"allowed": [], "bots": [],
                  "admission": {"issue_comment": ["inferred", "ignore", "ignore"], "pr_comment": ["inferred", "ignore", "ignore"],
                                "new_issue": ["inferred", "ignore", "ignore"], "new_pr": ["inferred", "ignore", "ignore"]}},
        "chat": {"allowed": [], "bots": [], "admission": {"dm": ["inferred", "ignore", "ignore"], "group": ["inferred", "ignore", "ignore"]}},   # chat tool (docs/002): listed people only
        "github": {"allowed": [], "bots": [],                                          # the same shape for every code host Connector (docs/002)
                   "admission": {"issue_comment": ["inferred", "ignore", "ignore"], "pr_comment": ["inferred", "ignore", "ignore"],
                                 "new_issue": ["inferred", "ignore", "ignore"], "new_pr": ["inferred", "ignore", "ignore"]}},
    },
}


class Policy:
    """Versioned rules + per-Source parts. The gate is code; the assessment is the model."""
    def __init__(self, store):
        self.store = store
        cur = store.kv_get("policy")
        if cur is None:
            cur = json.loads(json.dumps(DEFAULT_POLICY)); self._save(cur, origin="default")
        for k, v in DEFAULT_POLICY["sources"].items():                                 # a Source added after this policy was saved gets its default section
            cur.setdefault("sources", {}).setdefault(k, json.loads(json.dumps(v)))
        for k, v in DEFAULT_POLICY["defaults"].items(): cur.setdefault("defaults", {}).setdefault(k, v)      # a default added after this policy was saved (the daily limit) is there too, not a KeyError in the first request that reads it
        self.p = cur

    @staticmethod
    def version_of(p): return "P-" + hashlib.sha256(jdump(p).encode()).hexdigest()[:10]

    @property
    def version(self): return self.version_of(self.p)

    def _save(self, p, origin):
        v = self.version_of(p); self.store.kv_set("policy", p)
        rid = v if self.store.user == "default" else f"{self.store.user}/{v}"          # the same content hash may be recorded once per user
        if not self.store.get("policy_versions", rid):
            self.store.insert("policy_versions", {"id": rid, "policy": p, "origin": origin, "created": now()})
        return v

    def update(self, body, origin="panel"):
        p = json.loads(json.dumps(self.p))
        if "mode" in body:
            if body["mode"] not in ("off", "observe", "auto"): raise ValueError("mode must be off|observe|auto")
            p["mode"] = body["mode"]
        if "rules" in body:
            rules = []
            for r in body["rules"]:
                if not r.get("name") or not r.get("text"): raise ValueError("rule needs name and text")
                rules.append({"name": str(r["name"]), "text": str(r["text"]), "enabled": bool(r.get("enabled", True))})
            p["rules"] = rules
        if "defaults" in body: p["defaults"].update({k: body["defaults"][k] for k in ("budget_usd", "daily_budget_usd", "network", "version", "scope") if k in body["defaults"]})
        if "schedules" in body:
            scheds = []
            for i, sc in enumerate(body["schedules"]):
                if not sc.get("text"): raise ValueError("schedule needs text")
                if not (sc.get("every_s") or sc.get("daily_at")): raise ValueError("schedule needs every_s or daily_at")
                scheds.append({"id": str(sc.get("id") or f"s{i + 1}"), "text": str(sc["text"]), "every_s": int(sc["every_s"]) if sc.get("every_s") else None,
                               "daily_at": str(sc["daily_at"]) if sc.get("daily_at") else None, "enabled": bool(sc.get("enabled", True)), "budget_usd": float(sc.get("budget_usd") or p["defaults"]["budget_usd"])})
            p["schedules"] = scheds
        for src, part in (body.get("sources") or {}).items():
            cur = p["sources"].setdefault(src, {"allowed": [], "bots": []})
            for k in ("allowed", "bots"):
                if k in part: cur[k] = [str(x) for x in part[k]]
            if "admission" in part:
                for row, cells in part["admission"].items():
                    if len(cells) != 3 or any(c not in ("explicit", "inferred", "ignore") for c in cells): raise ValueError(f"admission row {row}: 3 cells of explicit|inferred|ignore")
                cur.setdefault("admission", {}).update(part["admission"])
        old = self.version; self.p = p; v = self._save(p, origin)
        return {"version": v, "changed": v != old}

    def enabled_rules(self): return [r for r in self.p["rules"] if r.get("enabled", True)]

    def source(self, kind): return self.p["sources"].get(kind, {"allowed": [], "bots": []})

    def is_allowed(self, kind, actor):
        al = self.source(kind).get("allowed", [])
        return "*" in al or actor in al

    def is_listed_bot(self, kind, actor): return actor in self.source(kind).get("bots", [])

    def gate(self, trigger, thread_has_tickets):
        """-> (strength|None, basis). Deterministic; no model."""
        kind, actor, human, addressed, scope = trigger["source"], trigger["actor"], trigger["human"], trigger["addressed"], trigger.get("scope", "")
        if not human:
            if self.is_listed_bot(kind, actor) and (addressed or thread_has_tickets): return ("inferred", "listed-bot") if self.p["mode"] != "off" else (None, "bot inferred off")
            return None, "bot not listed"
        listed = self.is_allowed(kind, actor)
        if listed and addressed: return "explicit", "listed-addressed"
        if listed and thread_has_tickets: return "explicit", "listed-followup"
        table = self.source(kind).get("admission", {})
        cells = table.get(scope)
        if cells is None: cells = ["inferred" if listed else "ignore", "ignore", "ignore"]
        cell = cells[0] if listed else (cells[1] if addressed else cells[2])
        if cell == "explicit": return "explicit", f"table:{scope}"
        if cell == "inferred":
            if thread_has_tickets and not listed and self.p["mode"] == "off": return None, "inferred off"
            return ("inferred", f"table:{scope}") if self.p["mode"] != "off" else (None, "inferred off")
        if thread_has_tickets and not listed: return ("inferred", "unlisted-followup") if self.p["mode"] != "off" else (None, "inferred off")
        return None, f"table:{scope}:ignore"
