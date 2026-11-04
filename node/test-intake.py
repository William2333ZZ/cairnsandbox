#!/usr/bin/env python3
"""Intake layer end to end (docs/005, 005): control plane + model proxy (mock) + intake, through the CP's /intake forwarding.
usage: test-intake.py <cp-url> [gitea-url gitea-token-file repo]
Light: one 512 MiB sandbox per ticket, sequential. Without the Gitea arguments the repository checkout is skipped:
the intake service must then run with INTAKE_REPO_TAR pointing at a tar (the test pushes a tiny repo via /triggers meta)."""
import atexit, json, sys, time, urllib.request

CP = sys.argv[1].rstrip("/")
_RES = {"ok": 0, "fail": 0}
def step(name, cond, detail=""):
    _RES["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}", flush=True)
atexit.register(lambda: print(f"RESULT ok={_RES['ok']} fail={_RES['fail']}"))

def req(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(CP + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read(); return resp.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        b = e.read(); return e.code, (json.loads(b) if b[:1] == b"{" else {"raw": b[:200].decode(errors="replace")})

def wait_ticket(tid, states=("ended",), timeout=600):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        c, t = req("GET", f"/intake/tickets/{tid}")
        if c == 200 and (t["state"] in states and not t.get("running")): return t, round(time.monotonic() - t0, 1)
        time.sleep(3)
    return t, round(time.monotonic() - t0, 1)

def decision_for(trig_id, timeout=25):
    """The gate's decision on one trigger. People's messages wait in a merge window (docs/005) before the gate sees them, so poll."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        req("POST", "/intake/pass")
        c, d = req("GET", "/intake/decisions")
        for x in d.get("decisions") or []:
            if trig_id in (x.get("triggers") or []): return x
        time.sleep(1)
    return {}

def ticket_of_thread(thread):
    for _ in range(30):
        req("POST", "/intake/pass")
        c, th = req("GET", "/intake/threads/" + urllib.parse.quote(thread, safe=""))
        if c == 200 and th.get("tickets"): return th["tickets"][0]["id"], th
        time.sleep(1)
    return None, th

import urllib.parse
BEFORE = {x["id"] for x in req("GET", "/sandboxes")[1].get("sandboxes", [])}
print("== services ==")
c, h = req("GET", "/intake/health"); step("intake reachable through /intake", c == 200 and h.get("ok") and h.get("cp"), json.dumps(h))
c, p = req("GET", "/intake/policy"); step("policy has a version and observe mode by default", c == 200 and p.get("version", "").startswith("P-") and p.get("mode") in ("observe", "auto", "off"), f"{p.get('version')} mode={p.get('mode')}")
c, a = req("GET", "/intake/attention"); step("attention view answers", c == 200 and "needs_person" in a and "counts" in a, json.dumps(a.get("counts")))

print("== gate ==")
tu = f"t-unlisted-{int(time.time())}"
c, r = req("POST", "/intake/triggers", {"id": tu, "source": "gitea", "actor": "stranger", "human": True, "addressed": True, "scope": "issue_comment",
                                        "text": "@cairn-bot do something", "threads": [f"gitea:test#{int(time.time())}"], "channel": {"kind": "none"}, "meta": {}})
step("unlisted person's trigger is stored and queued", c == 201 and r.get("disposition") == "queued", json.dumps(r))
last = decision_for(tu)
step("gate ignores it: no ticket, decision recorded", last.get("outcome") == "ignore" and last.get("ticket") is None, f"{last.get('outcome')} {last.get('basis')} {last.get('reason')}")
tb = f"t-bot-{int(time.time())}"
c, r = req("POST", "/intake/triggers", {"id": tb, "source": "gitea", "actor": "fc-runner", "human": False, "addressed": True, "scope": "issue_comment",
                                        "text": "CI failed", "threads": [f"gitea:test#{int(time.time())}b"], "channel": {"kind": "none"}, "meta": {}})
last = decision_for(tb)
step("unlisted bot is not admitted", last.get("outcome") == "ignore" and "bot" in (last.get("basis") or ""), f"{last.get('basis')}")

print("== policy versions ==")
v0 = p["version"]
c, r = req("PUT", "/intake/policy", {"rules": [{"name": f"noop-rule-{int(time.time())}", "text": "never matches: only for the version test", "enabled": False}]})   # unique: the state dir may persist between runs
step("policy change produces a new version", c == 200 and r.get("changed") and r.get("version") != v0, json.dumps(r))
c, pv = req("GET", "/intake/policy/versions"); step("version history kept", c == 200 and len(pv.get("versions", [])) >= 2, f"{len(pv.get('versions', []))} versions")

print("== explicit request from the workbench -> ticket -> sandbox -> report ==")
conv = f"ci-{int(time.time())}"
c, r = req("POST", "/intake/requests", {"text": "List the repository files and finish. Do not change anything.", "conversation": conv, "actor": "ci"})
step("request accepted as a trigger", c == 201 and r.get("disposition") == "queued", json.dumps(r))
tid, th = ticket_of_thread(f"workbench:{conv}")
step("explicit request became a ticket without a model call", bool(tid) and any(d["outcome"] == "accept" and d["by_whom"] == "gate" for d in th.get("decisions", [])), f"ticket={tid}")
t, s = wait_ticket(tid)
lr = t.get("last_report") or {}
step("assignment ran in a sandbox and the harness reported through the ABI", t.get("assignments") and t["assignments"][0]["sandbox"] and t["assignments"][0]["reports"], f"{s}s reports={[r.get('state') for r in (t['assignments'][0].get('reports') or [])]} exit={t['assignments'][0].get('agent_exit')} err={t['assignments'][0].get('error')}")
step("ticket ended ready (mock model submits without commits)", t["state"] == "ended" and t.get("outcome") == "ready" and lr.get("state") == "ready", f"state={t['state']} outcome={t.get('outcome')} last={lr.get('state')}: {lr.get('text', '')[:80]}")
step("workspace packed into a layer for later assignments", bool(t.get("workspace_layer")), t.get("workspace_layer"))
step("ticket carries token usage from the proxy", (t["assignments"][0].get("tokens_in") or 0) > 0, f"tokens {t['assignments'][0].get('tokens_in')}/{t['assignments'][0].get('tokens_out')}")
step("acknowledgement and result replies queued on the ticket", {m["purpose"] for m in t.get("replies", [])} >= {"acknowledgement", "result"}, str([m["purpose"] for m in t.get("replies", [])]))
step("no credentials in the sandbox log", "token" not in (t["assignments"][0].get("error") or "").lower(), "")

print("== follow-up resumes the ended ticket ==")
c, r = req("POST", "/intake/requests", {"text": "Thanks. Now also list hidden files and finish.", "conversation": conv, "actor": "ci"})
step("follow-up on the thread fans out to the ticket (no new eval)", c == 201 and r.get("disposition") == "fanned-out", json.dumps(r))
t2, s = wait_ticket(tid, states=("open",), timeout=60)
c, t2 = req("GET", f"/intake/tickets/{tid}")
t3, s = wait_ticket(tid)
step("resumed ticket ran a recovery assignment on the workspace layer and ended again", len(t3.get("assignments", [])) >= 2 and t3["assignments"][-1].get("recovery") and t3["state"] == "ended", f"{s}s assignments={len(t3.get('assignments', []))} layers={t3['assignments'][-1].get('layers')}")

print("== stop by a person ==")
conv2 = conv + "-stop"
c, r = req("POST", "/intake/requests", {"text": "Start something long: sleep 1; then finish.", "conversation": conv2, "actor": "ci"})
tid2, th2 = ticket_of_thread(f"workbench:{conv2}")
c, r = req("POST", f"/intake/tickets/{tid2}/stop", {"reason": "test"})
c, t4 = req("GET", f"/intake/tickets/{tid2}")
step("panel stop ends the ticket as person-stopped", c == 200 and t4["state"] == "ended" and t4["ended_by"] == "person", f"{t4.get('ended_by')} {t4.get('outcome')}")
c, r = req("POST", "/intake/requests", {"text": "please continue", "conversation": conv2, "actor": "ci"}); req("POST", "/intake/pass"); time.sleep(1)
c, t5 = req("GET", f"/intake/tickets/{tid2}")
step("a person-stopped ticket does not resume on a follow-up", t5["state"] == "ended", f"disposition={r.get('disposition')} state={t5['state']}")
tid3, _ = ticket_of_thread(f"workbench:{conv2}")

print("== cleanup ==")
for _ in range(20):
    c, a = req("GET", "/intake/attention")
    if a.get("counts", {}).get("running", 1) == 0: break
    time.sleep(3)
for t in req("GET", "/intake/tickets?state=open")[1].get("tickets", []): req("POST", f"/intake/tickets/{t['id']}/stop", {"reason": "cleanup"})
left = None
for _ in range(30):                      # an assignment that was mid-create when its ticket was stopped is destroyed when its run ends
    c, a = req("GET", "/intake/attention"); c2, sb = req("GET", "/sandboxes")
    left = len([s for s in sb.get("sandboxes", []) if s.get("class") == "persistent"]) if c2 == 200 else None   # intake's sandboxes are persistent; an evaluator may be running ephemeral ones on the same node
    if a.get("counts", {}).get("running", 1) == 0 and left == 0: break
    time.sleep(3)
c_, sb_ = req("GET", "/sandboxes"); left_ = [x["id"] for x in sb_.get("sandboxes", []) if x["id"] not in BEFORE]   # the node is shared: only what appeared during this run counts
step("no sandboxes left behind", c_ == 200 and not left_, f"{len(left_)} left {left_[:3]}")
