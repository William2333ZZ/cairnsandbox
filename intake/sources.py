# Triggers that are not a person on a code host: the platform itself (system), the workbench, CI results, routines on a schedule (docs/005 例行与自动 Source).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *

# ----------------------------------------------------------------------------------------------- sources

def system_trigger(source, thread, text, title, channel=None, meta=None, budget_usd=None):
    """A Trigger from the platform itself (ci / evaluation / schedule): explicit, addressed, actor "system"."""
    tid = f"{source}-" + hashlib.sha256((thread + "\n" + text).encode()).hexdigest()[:12]
    meta = dict(meta or {}); meta["title"] = title[:80]
    if budget_usd: meta["budget_usd"] = budget_usd
    return {"id": tid, "source": source, "actor": "system", "human": True, "addressed": True, "scope": "request", "text": text, "url": None,
            "threads": [thread], "channel": channel or {"kind": "workbench", "conversation": thread}, "meta": meta}


def workbench_trigger(body, actor="workbench"):
    text = (body.get("text") or "").strip()
    if not text: raise ValueError("text required")
    conv = body.get("conversation") or new_id("c")
    th = f"workbench:{conv}"
    meta = {k: body[k] for k in ("version", "budget_usd", "repo") if k in body}
    meta["title"] = text[:80]
    return {"id": "wb-" + hashlib.sha256((th + text + str(now())).encode()).hexdigest()[:12], "source": "workbench", "actor": body.get("actor") or actor, "human": True,
            "addressed": True, "scope": "request", "text": text, "url": None, "threads": [th], "channel": {"kind": "workbench", "conversation": conv}, "meta": meta}


class CISource:
    """main's commit status on Gitea (the runner reports each job): a failed job becomes one ticket per commit."""
    def __init__(self, engine): self.e = engine; self.g = engine.gitea; self._stop = threading.Event()

    def poll_once(self):
        g = self.g
        br = g.api("GET", f"/repos/{g.repo}/branches/{g.base}"); sha = ((br.get("commit") or {}).get("id") or "")[:40]
        if not sha: return 0
        st = g.api("GET", f"/repos/{g.repo}/commits/{sha}/status")
        if st.get("state") != "failure": return 0
        failed = [s for s in st.get("statuses", []) if s.get("status") in ("failure", "error")]
        if not failed: return 0
        if self.e.over_daily_budget(): return 0                                           # not marked seen: when the limit is lifted (tomorrow, a higher limit, the balance back) a still-red main is looked at
        seen = self.e.store.kv_get("ci.seen", [])
        if sha in seen: return 0
        if self.e.store.all("tickets", "WHERE state='open' AND source='ci'"): return 0                 # one at a time: a red main that an open job has not fixed yet does not need a second job (and the next sha is looked at when this one closes)
        text = f"CI failed on {g.base} at {sha[:10]}: " + "; ".join(f"{s.get('context')} ({s.get('description')})" for s in failed) + \
               f"\nFind the cause in the repository and fix it; keep the change minimal and commit it. The run: {g.url}{failed[0].get('target_url', '')}"
        t = system_trigger("ci", f"ci:{sha[:10]}", text, f"CI failed at {sha[:10]}", meta={"commit": sha}, budget_usd=0.3)
        self.e.receive(t); self.e.store.kv_set("ci.seen", (seen + [sha])[-50:]); return 1

    def start(self):
        if not self.g.enabled: return
        def loop():
            while not self._stop.is_set():
                try: self.poll_once(); self.e.problems.pop("ci", None)
                except Exception as e: self.e.problems["ci"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}  # noqa: BLE001
                self._stop.wait(120)
        threading.Thread(target=loop, daemon=True).start()


class ScheduleSource:
    """Timed requests from the policy's schedules: every N seconds, or daily at HH:MM (host local time)."""
    def __init__(self, engine): self.e = engine; self._stop = threading.Event()

    def due(self, sc, last):
        if sc.get("every_s"): return now() - (last or 0) >= sc["every_s"]
        if sc.get("daily_at"):
            hh, mm = (sc["daily_at"] + ":0").split(":")[:2]; lt = time.localtime()
            today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hh), int(mm), 0, 0, 0, -1))
            return now() >= today and (last or 0) < today
        return False

    def poll_once(self):
        if self.e.over_daily_budget(): return 0                                           # the routine is not marked as having run, so it runs when the limit allows
        fired = 0; last_map = self.e.store.kv_get("schedule.last", {})
        for sc in self.e.policy.p.get("schedules", []):
            if not sc.get("enabled", True) or not self.due(sc, last_map.get(sc["id"])): continue
            day = time.strftime("%Y-%m-%d-%H%M")
            t = system_trigger("schedule", f"schedule:{sc['id']}:{day}", sc["text"], f"定时:{sc['text'][:60]}", meta={"schedule": sc["id"]}, budget_usd=sc.get("budget_usd"))
            self.e.receive(t); last_map[sc["id"]] = now(); fired += 1
        if fired: self.e.store.kv_set("schedule.last", last_map)
        return fired

    def start(self):
        def loop():
            while not self._stop.is_set():
                try: self.poll_once()
                except Exception as e: self.e.problems["schedule"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}  # noqa: BLE001
                self._stop.wait(30)
        threading.Thread(target=loop, daemon=True).start()
