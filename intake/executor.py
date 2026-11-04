# Assignments: sandbox, prepare, run, result, Report, watchdog (docs/006).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError
from handoff import SelfHandoff
from rulecheck import patch_facts
from home import HOME_IN_SANDBOX


class ExecutorMixin:
    # ---- assignments
    def _new_assignment(self, tid, recovery):
        with self.lock:
            if any(a["state"] in ("pending", "running") for a in self.store.all("assignments", "WHERE ticket=?", (tid,))): return None   # one at a time per Ticket
            t = self.store.get("tickets", tid)
            a = {"id": new_id("a"), "ticket": tid, "state": "pending", "sandbox": None, "node": None, "version": (lambda v: v.get("id") if isinstance(v, dict) else v)(t["mandate"].get("version")), "layers": None, "reports": [], "events": [],
                 "agent_exit": None, "cost": 0.0, "tokens_in": 0, "tokens_out": 0, "elapsed_s": None, "error": None, "log": None, "recovery": recovery, "created": now(), "started": None, "ended": None}
            self.store.insert("assignments", a)
        threading.Thread(target=self._run_assignment, args=(a["id"],), daemon=True).start()
        return a["id"]

    def _layers_for(self, t):
        layers = list(self.cfg["layers"])
        v = t["mandate"].get("version")
        if isinstance(v, dict) and v.get("layers"): layers = list(v["layers"]) + [l for l in layers if l not in v["layers"]]
        if t.get("workspace_layer"): layers.append(t["workspace_layer"])
        return layers

    def _run_assignment(self, aid):
        a = self.store.get("assignments", aid); t = self.store.get("tickets", a["ticket"])
        human_wake = bool(a["recovery"]) and any(i.get("human") and not i.get("delivered") for i in (t.get("inputs") or []))
        sem = self.slots                                   # a person's message may take the reserve slot when the normal ones are full
        if not (human_wake and self.slots.acquire(blocking=False)):
            if human_wake: sem = self.reserve
            sem.acquire()
        try:
            with self.lock: t = self.store.get("tickets", a["ticket"]); a = self.store.get("assignments", aid)     # the wait may have been long: fresh state
            if self.paused or t["state"] != "open" or a["state"] != "pending": self.store.update("assignments", aid, {"state": "ended", "ended": now(), "error": "ticket not open"}); return
            self.store.update("assignments", aid, {"state": "running", "started": now()})
            res = self._execute(aid, t)
            self._apply_result(aid, t, res)
        except InfraError as e:
            self.store.update("assignments", aid, {"state": "ended", "ended": now(), "error": f"infra: {e}"[:400]})
            since_human = max([i.get("ts", 0) for i in (t.get("inputs") or []) if i.get("human")] or [0])
            infra_fails = [x for x in self.store.all("assignments", "WHERE ticket=?", (t["id"],)) if str(x.get("error") or "").startswith("infra:") and (x.get("created") or 0) >= since_human]
            if len(infra_fails) < 3:                      # docs/006: three automatic retries with growing backoff, then the person
                delay = (5, 30, 120)[min(len(infra_fails), 2)]
                threading.Timer(delay, lambda: self._new_assignment(t["id"], recovery=True)).start()
            else:
                self._report(t["id"], aid, {"state": "failed", "text": f"infrastructure failure ({len(infra_fails)} attempts): {e}"[:400]})
        except Exception as e:  # noqa: BLE001
            self.store.update("assignments", aid, {"state": "ended", "ended": now(), "error": f"{type(e).__name__}: {e}"[:400]})
            self._report(t["id"], aid, {"state": "failed", "text": f"intake error: {type(e).__name__}: {e}"[:400]})        # said, not swallowed: a job that fails inside the host is stuck, and the person hears it
        finally:
            self.running.pop(t["id"], None); sem.release()

    def _execute(self, aid, t):
        cp, cfg = self.cp, self.cfg
        hosts = [urllib.parse.urlparse(cfg["model_proxy"]).hostname] if cfg["model_proxy"] else []
        spec = {"class": "persistent", "template": cfg["template"], "layers": self._layers_for(t), "vcpu": 1, "mem": 512, "disk": cfg["disk_mib"],
                "ttl_max_s": cfg["assignment_timeout_s"] + 600, "ttl_idle_s": cfg["assignment_timeout_s"] + 600, "network": {"allow_hosts": [h for h in hosts if h]}}
        if cfg.get("project"): spec["project"] = cfg["project"]                          # tenancy: usage and quotas land on this Project, not the token's default
        home_vol, home_digest = (None, "")
        if cfg.get("home_volume"):                                                   # D35: the directory rides as a read-only volume, attached at create (volumes boot cold)
            home_vol, home_digest = self.home_volume()
            if home_vol: spec["volumes"] = [{"id": home_vol, "mount": HOME_IN_SANDBOX, "ro": True}]
        sid, node = cp.create(spec); self.running[t["id"]] = sid
        self.store.update("assignments", aid, {"sandbox": sid, "node": node, "layers": spec["layers"]})
        t0 = now()
        try:
            cp.budget(sid, t["mandate"]["budget_usd"])
            cwd, base_commit = self._prepare(sid, t)
            if not home_vol: home_digest = self.home_ship(sid)                           # the person's harness directory (docs/004, D29): read-only at /work/home, named by its digest
            self.store.update("assignments", aid, {"home": home_digest, "home_volume": home_vol})
            with self.lock: t = self.store.get("tickets", t["id"])                       # inputs that arrived during the wait and the prepare
            inputs = t.get("inputs") or []
            text_items = [i for i in inputs if i.get("kind") in ("trigger", "answer")]
            def say_of(i):                                                                # words that did not come from the person are data, and are wrapped as such
                body = (f"Answer from {i.get('actor', 'a person')}: " if i.get("kind") == "answer" else "") + (i.get("text") or "")
                return unverified(i.get("actor", "?"), body) if i.get("trusted") is False else body
            others = [{"name": a["name"], "role": a.get("role") or ""} for a in (self.engines.agents(self.cfg.get("owner") or self.user) if getattr(self, "engines", None) else []) if a["name"] != (cfg.get("agent") or "main")]
            ticket = {"id": t["id"], "short": t["short"], "text": "\n\n".join(say_of(i) for i in text_items)[:8000], "agents": others,
                      "scope": t["mandate"].get("scope"), "branch": t["branch"], "base_branch": cfg["base_branch"], "base_commit": base_commit, "cwd": cwd,
                      "head_at_start": cp.exec(sid, f"git -c safe.directory='*' -C {cwd} rev-parse HEAD", user="root", timeout=20)["stdout"].strip(),
                      "rules": [r for r in self.policy.enabled_rules() if r["name"] in (t["mandate"].get("matched_rules") or [])],
                      "inputs": [({**i, "text": unverified(i.get("actor", "?"), i.get("text") or "")} if i.get("trusted") is False else i) for i in text_items[1:][-10:]], "thread": t["thread"], "memory": self.memory_for(t), "prompts": self.prompt_overrides(),
                      "agent": cfg.get("agent") or "main", "role": self.store.kv_get("agent.role", "") or "", "home": home_digest}        # docs/001 多个 agent: this agent's role, as information
            cp.ticket(sid, ticket)
            delivered_ts = {i.get("ts") for i in inputs}
            with self.lock:                                                               # mark by identity: a later arrival keeps delivered=False
                t = self.store.get("tickets", t["id"])
                for i in t.get("inputs") or []:
                    if i.get("ts") in delivered_ts: i["delivered"] = True
                self.store.update("tickets", t["id"], {"inputs": t["inputs"]})
            env = {"CAIRN_MODEL_NAME": cfg["model_name"]}
            if home_digest: env["CAIRN_HOME"] = HOME_IN_SANDBOX
            if self.proxy: env["CAIRN_MODEL_URL"], env["CAIRN_MODEL_KEY"] = cfg["model_proxy"] + "/v1", self.proxy.token(sid, t["mandate"]["budget_usd"])
            cmd = DEFAULT_AGENT_CMD.replace("{out}", "/tmp/aios/traj.json").replace("{cwd}", cwd)      # /tmp is writable under the default Landlock policy; /work/.aios is not
            ar = cp.exec(sid, cmd, user="agent", timeout=cfg["assignment_timeout_s"], env=env, cwd=cwd, landlock=True if cfg["landlock"] else None)
            self.running.pop(t["id"], None)                                               # the harness is done: later inputs are not delivered here
            events = cp.events_strict(sid) if hasattr(cp, "events_strict") else cp.events(sid)
            reports = [e["data"] for e in events if e.get("type") == "report"]
            last = reports[-1] if reports else None
            if last is None:
                last = {"state": "failed", "text": f"no report; harness exit {ar['exit']}" + (" (timeout)" if ar["timeout"] else "")} if ar["exit"] != 0 or ar["timeout"] else {"state": "ready", "text": "finished without a report"}
            patch = None
            if last.get("state") == "ready" and last.get("deliverable"):
                r = cp.exec(sid, f"git -c safe.directory='*' -C {cwd} format-patch --stdout {base_commit}..HEAD > /work/.aios/ticket.patch", user="root", timeout=120)
                if r["exit"] == 0: patch = cp.get(sid, "/work/.aios/ticket.patch")
            usage = self.proxy.usage(sid) if self.proxy else {}
            try: traj = json.loads(cp.get(sid, "/tmp/aios/traj.json")); traj = {"messages": traj.get("messages", [])[-400:], "info": traj.get("info")}
            except (InfraError, ValueError): traj = None
            layer = None
            try: layer = (cp.pack(sid, f"ws-{t['short']}") or {}).get("id")
            except InfraError as e: self.problems["pack"] = {"error": str(e)[:200], "since": now()}
            return {"report": last, "reports": reports, "events": events[-300:], "agent_exit": ar["exit"], "timeout": ar["timeout"], "patch": patch, "base_commit": base_commit, "traj": traj,
                    "log": (ar["stdout"][-3000:] + "\n--- stderr ---\n" + ar["stderr"][-3000:]), "elapsed_s": now() - t0, "layer": layer,
                    "cost": float(usage.get("cost_usd") or 0), "tokens_in": int(usage.get("tokens_in") or 0), "tokens_out": int(usage.get("tokens_out") or 0), "tokens_cached": int(usage.get("tokens_cached") or 0)}
        finally:
            cp.destroy(sid)
            if home_vol: self.home_volume_gc(home_vol)

    def _prepare(self, sid, t):
        """Repository checkout at /work/repo on branch ticket/<short>. No credentials enter the sandbox."""
        cp = self.cp; cwd = "/work/repo"
        if t.get("workspace_layer"):
            G = f"git -c safe.directory='*' -C {cwd}"                      # the checkout is owned by agent; root runs these
            r = cp.exec(sid, f"test -d {cwd}/.git && {G} rev-parse --verify {t['branch']} >/dev/null 2>&1 && echo ok", user="root", timeout=30)
            if "ok" in r["stdout"]:
                cp.exec(sid, f"{G} checkout -q {t['branch']}; chown -R agent:agent /work", user="root", timeout=60)
                base = cp.exec(sid, f"{G} rev-parse {self.cfg['base_branch']}", user="root", timeout=20)["stdout"].strip()
                cp.put(sid, "/work/.aios/RECOVERY.md", b"Previous assignment ended; check the directory, git log and the thread before repeating outside actions.\n")
                return cwd, base
        if not self.gitea.readable: raise InfraError("no repository configured (GITEA_URL/GITEA_REPO)")
        tar, base_commit = self.gitea.clone_tar(self.cfg["clone_depth"])
        if len(tar) > 150 << 20: raise InfraError(f"repository checkout is {len(tar) >> 20} MiB; too large to ship into a sandbox")
        cp.put(sid, "/tmp/repo.tar", tar)
        r = cp.exec(sid, f"mkdir -p /work/.aios && tar -C /work -xf /tmp/repo.tar && rm -f /tmp/repo.tar && git -C {cwd} checkout -q -b {t['branch']} && "
                         f"git -C {cwd} config user.name aios && git -C {cwd} config user.email aios@sandbox.local && chown -R agent:agent /work", user="root", timeout=180)
        if r["exit"] != 0: raise InfraError(f"prepare failed: {r['stderr'][-300:]}")
        return cwd, base_commit

    def _apply_result(self, aid, t, res):
        upd = {"state": "ended", "ended": now(), "reports": res["reports"], "events": res["events"], "agent_exit": res["agent_exit"], "cost": res["cost"],
               "tokens_in": res["tokens_in"], "tokens_out": res["tokens_out"], "tokens_cached": res.get("tokens_cached", 0), "elapsed_s": res["elapsed_s"], "log": res["log"], "traj": res.get("traj")}
        self.store.update("assignments", aid, upd)
        if res.get("layer"): self.store.update("tickets", t["id"], {"workspace_layer": res["layer"]})
        try: self.memory_after(t, res)
        except Exception as e: self.problems["memory"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}  # noqa: BLE001
        rep = dict(res["report"]); rep["patch"] = res.get("patch"); rep["base_commit"] = res.get("base_commit")
        self._report(t["id"], aid, rep)
        t2 = self.store.get("tickets", t["id"])
        if t2 and t2["state"] == "open" and any(i.get("human") and not i.get("delivered") for i in (t2.get("inputs") or [])):
            self._new_assignment(t["id"], recovery=True)                                 # a person wrote while the harness was finishing

    def _report(self, tid, aid, rep):
        """Apply one Report to its Ticket (docs/006-execution.md: delivery, attention, end)."""
        with self.lock:
            t = self.store.get("tickets", tid)
            if t["state"] == "ended" and t.get("ended_by") in ("person", "handoff"): return      # stopped or handed over meanwhile: nothing to apply, nothing to say
            state = rep.get("state") if rep.get("state") in REPORT_STATES else "failed"
            att = rep.get("attention"); rep = {**rep, "attention": att if isinstance(att, dict) else ({"question": str(att)[:500]} if att else None)}      # whatever shape the harness sent, it is an object from here on
            upd = {"last_report": {"state": state, "text": rep.get("text", ""), "at": now(), "assignment": aid}, "updated": now()}; restart = False
            if state == "waiting" and (rep.get("attention") or {}).get("kind") == "handoff":
                a = rep["attention"]
                try: self.handoff_ticket(t, a.get("to"), a.get("brief") or rep.get("text"))
                except SelfHandoff:                                          # the work is already this agent's: say so and let it carry on, instead of bothering the person
                    upd["inputs"] = (t.get("inputs") or []) + [{"kind": "answer", "actor": "system", "text": f"This job was handed to you (you are @{self.cfg.get('agent') or 'main'}); there is nobody else to hand it to. Do it yourself now and finish normally.", "human": True, "trusted": True, "ts": now(), "delivered": False}]
                    upd["attention"] = None; restart = True
                except ValueError as e:
                    upd["last_report"] = {"state": "blocked", "text": f"hand-off refused: {e}"[:300], "at": now(), "assignment": aid}
                    self.say(t, "stuck", f"想把这件事交给 @{str(a.get('to'))[:30]},但交不出去:{str(e)[:200]}。{AGENT_LINE}")
            elif state == "waiting" and rep.get("attention") and (rep["attention"] or {}).get("kind") == "approval":
                a = rep["attention"] or {}; act = a.get("action") if isinstance(a.get("action"), dict) else {}
                kind = act.get("kind") if act.get("kind") in APPROVAL_KINDS else "send"; draft = (a.get("draft") or rep.get("text") or "")[:1000]
                self._approval(t, kind, self._approval_action(t, kind, act, draft), draft)
            elif state == "waiting" and not rep.get("attention"):
                why = (rep.get("text") or "stopped")[:200]              # the harness stopped without finishing (a step or time limit): left alone, the job would sit open and silent forever
                upd["attention"] = {"question": f"它停下来了,事没做完({why})。回复「继续」让它接着做,或补一句说明;不需要就停掉。", "asked": now(), "assignment": aid}
                self.say(t, "stuck", f"Stopped before finishing: {why}. Reply to let it continue, or stop it. {AGENT_LINE}")
            elif state == "waiting" and rep.get("attention"):
                upd["attention"] = {"question": (rep["attention"] or {}).get("question") or rep.get("text", ""), "asked": now(), "assignment": aid}
                self.say(t, "question", f"Question: {upd['attention']['question'][:400]} {AGENT_LINE}")
            elif state == "ready" and self.rule_gate(t, rep, upd): restart = True
            elif state == "ready":
                delivered = False
                touched = sorted({p for p in patch_facts(rep["patch"])["files"] if p.startswith(PROTECTED_PATHS)}) if rep.get("patch") else []      # every path the patch names, old and new: deleting, renaming out of or chmod-ing a protected file counts
                if touched:
                    rep["patch"] = None; state = "blocked"
                    upd["last_report"] = {"state": "blocked", "text": f"patch touches protected paths ({', '.join(touched)}); a person must apply it by hand", "at": now(), "assignment": aid}
                    self.say(t, "stuck", f"Finished, but the change touches {', '.join(touched)}, which the relay never pushes. {AGENT_LINE}")
                if state == "ready" and rep.get("deliverable") and rep.get("patch") and self.gitea.enabled:
                    try:
                        head = self.gitea.push_patch(t["branch"], rep["patch"], rep.get("base_commit") or "HEAD")
                        if (t.get("pr") or {}).get("number") and (t["pr"].get("state") or "open") == "open":      # the branch already has a PR: it just moved
                            upd["pr"] = {**t["pr"], "head": head}
                            self.say(t, "ready", f"Updated PR #{t['pr']['number']} to {head[:10]}. {AGENT_LINE}"); delivered = True
                            self._approval(t, "merge", {"pr": t["pr"]["number"], "url": t["pr"].get("url"), "branch": t["branch"], "head": head}, f"合并 PR #{t['pr']['number']}:{t['title'][:80]}")      # the draft follows the PR: same row, looked at again, pinned to the new head
                        else:
                            pr = self.gitea.open_pr(t["branch"], f"[aios {t['short']}] {t['title'][:70]}", f"{AGENT_LINE}\n\n{rep.get('text', '')[:1500]}\n\nTicket `{t['id']}`; thread {t['thread']}.")
                            upd["pr"] = {"number": pr.get("number"), "url": pr.get("html_url"), "head": head, "state": "open"}
                            try: self.subscribe(t["id"], f"{getattr(self.gitea, 'kind', 'gitea')}:{self.gitea.repo}#{pr.get('number')}", "own", source=getattr(self.gitea, "kind", "gitea"))      # docs/001: the PR belongs to the Ticket; a later person's comment on it continues this work (dsh-superagent 003 §47)
                            except Exception as e: self.problems["subscribe"] = {"error": str(e)[:200], "since": now()}      # noqa: BLE001
                            self.say(t, "ready", f"Opened PR #{pr.get('number')} ({pr.get('html_url')}). {(rep.get('text') or '')[:300].strip()} {AGENT_LINE}".replace("  ", " ")); delivered = True
                            self._approval(t, "merge", {"pr": pr.get("number"), "url": pr.get("html_url"), "branch": t["branch"], "head": head}, f"合并 PR #{pr.get('number')}:{t['title'][:80]}")
                    except Exception as e:  # noqa: BLE001
                        upd["last_report"] = {"state": "blocked", "text": f"delivery failed: {type(e).__name__}: {e}"[:400], "at": now(), "assignment": aid}; state = "blocked"
                        self.say(t, "stuck", f"Finished, but could not open the pull request: {str(e)[:200]}. {AGENT_LINE}")
                if state == "ready" and not delivered:
                    self.say(t, "ready", f"{(rep.get('text') or 'Done.')[:600]} {AGENT_LINE}")
                    if (t.get("pr") or {}).get("number") and (t["pr"].get("state") or "open") == "open": pass          # a PR is still open: the job is not over until it is merged or closed
                    else: upd.update({"state": "ended", "ended_by": "executor", "outcome": "ready"})
            elif state == "dropped":
                self.say(t, "ready", f"Nothing to do here: {(rep.get('text') or '')[:300]} {AGENT_LINE}")
                upd.update({"state": "ended", "ended_by": "executor", "outcome": "dropped"})
            elif state in ("blocked", "failed") and BILLING_RE.search(rep.get("text") or ""):
                self.problems["billing"] = {"error": "the model provider refused: account balance", "since": now()}                       # not the job's fault and not retryable: the person has to act
                upd["attention"] = {"question": "模型服务商说账户余额不足,所有要调模型的事都停了。充值之后回复「继续」,它接着做。", "asked": now(), "assignment": aid}
                self.say(t, "stuck", f"The model provider says the account balance is used up, so nothing that calls the model can run. Top it up, then reply to continue. {AGENT_LINE}")
            elif state in ("blocked", "failed"):
                if not rep.get("infra"): self.say(t, "stuck", f"{state}: {(rep.get('text') or '')[:300]} {AGENT_LINE}")
            self.store.update("tickets", tid, upd)
            if upd.get("state") == "ended": self._return_unread(tid)
            if restart: self._new_assignment(tid, recovery=True)

    # ---- watchdog (docs/006: first version only reports; it never intervenes)
    STALL_S = 600; LOOP_WINDOW = 20; LOOP_RATIO = 0.6

    def watchdog(self):
        """Read each running sandbox's kernel event log: stall (no model call for STALL_S), loop (repeated commands), Landlock denials."""
        for tid, sid in list(self.running.items()):
            try: events = self.cp.events(sid)
            except InfraError: continue
            calls = [e for e in events if e.get("type") == "model_call"]
            try: self.intent_check(tid, sid, len(calls))
            except Exception as e:  # noqa: BLE001 - the second reader must never take the first one down
                self.problems["intent"] = {"error": f"{type(e).__name__}: {e}"[:200], "since": now()}
            last = max((e.get("ts") or 0) for e in calls) if calls else None
            started = min((e.get("ts") or now()) for e in events) if events else now()
            signals = []
            if (last or started) and now() - (last or started) > self.STALL_S: signals.append(f"stall: {int(now() - (last or started))}s without a model call")
            cmds = [a for e in calls[-self.LOOP_WINDOW:] for a in ((e.get("data") or {}).get("actions") or [])]
            if len(cmds) >= 8 and (1 - len(set(cmds)) / len(cmds)) > self.LOOP_RATIO: signals.append(f"loop: {len(cmds) - len(set(cmds))} repeated commands in the last {len(cmds)}")
            denied = sum(1 for e in events if e.get("type") == "kernel" and "landlock" in json.dumps(e.get("data") or {}).lower())
            if denied: signals.append(f"out of scope: {denied} denied accesses")
            key = f"watchdog:{tid}"
            if signals: self.problems[key] = {"error": "; ".join(signals)[:300], "since": self.problems.get(key, {}).get("since", now()), "ticket": tid}
            else: self.problems.pop(key, None)
