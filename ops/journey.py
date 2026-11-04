#!/usr/bin/env python3
"""A customer's whole journey against a real control plane (D68): invite -> signup -> API key -> a sandbox -> usage -> prepaid credit running out -> suspension
-> top-up -> offboarding. Leaves nothing behind (the account is deleted at the end; its name stays reserved, so pass a fresh name each run).

    CAIRN_URL=http://fc-cp:8700 CAIRN_TOKEN=<deployer token> python3 ops/journey.py [account-name]

One 128 MiB sandbox for a few seconds. The deployer token is read from the environment and never printed."""
import json, os, sys, time, urllib.error, urllib.request

URL = os.environ.get("CAIRN_URL", "http://127.0.0.1:18700").rstrip("/"); ADMIN = os.environ["CAIRN_TOKEN"]
NAME = sys.argv[1] if len(sys.argv) > 1 else "journey-" + str(int(time.time()))[-7:]
FAILED = []


def call(method, path, body=None, token=None, timeout=120):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read(); return e.code, (json.loads(raw) if raw[:1] == b"{" else {"raw": raw[:200].decode(errors="replace")})


def step(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}"); 
    if not cond: FAILED.append(name)


st, inv = call("POST", "/invites", {"note": "journey", "credit_usd": 0.5, "max_sandboxes": 2, "daily_usd": 1, "ttl_s": 600}, ADMIN)
step("operator issues an invite", st == 201 and "code" in inv, str(st))
st, su = call("POST", "/signup", {"code": inv["code"], "account": NAME, "user": NAME + "-owner"})
step("the customer signs up with the code (no token needed)", st == 201 and su.get("token"), f"{st} account={NAME}")
owner = su["token"]
st, bal = call("GET", f"/accounts/{NAME}/balance", token=owner)
step("the account starts prepaid with the invite's credit", st == 200 and bal.get("mode") == "prepaid" and abs(bal.get("balance_usd", 0) - 0.5) < 1e-9, json.dumps(bal)[:120])
st, k = call("POST", f"/projects/{NAME}/keys", {"name": "ci", "role": "member"}, owner)
step("the owner mints an API key", st == 201 and k.get("secret", "").startswith("cairn_"))
key = k["secret"]
st, me = call("GET", "/me", token=key)
step("the key is scoped to the account's project", me.get("key") is True and me.get("projects") == [NAME], json.dumps(me)[:100])
st, sb = call("POST", "/sandboxes", {"mem": 128, "class": "ephemeral", "ttl_max_s": 300}, key)
step("the key creates a sandbox", st == 201 and sb.get("project") == NAME, f"{st} {sb.get('id')}")
sid = sb["id"]
st, r = call("POST", f"/sandboxes/{sid}/exec", {"cmd": "echo journey"}, key)
step("and runs a command in it", st == 200 and r.get("stdout", "").strip() == "journey")
st, v = call("POST", "/projects/%s/keys" % NAME, {"name": "dash", "role": "viewer"}, owner)
viewer = v["secret"]
step("a viewer key can read but not write", call("GET", "/sandboxes", token=viewer)[0] == 200 and call("POST", "/sandboxes", {"mem": 128}, viewer)[0] == 403)
st, q = call("GET", f"/projects/{NAME}/quota", token=owner)
step("the invite's trial quota is in place", q.get("max_sandboxes") == 2 and q.get("daily_usd") == 1, json.dumps(q)[:100])
st, ab = call("GET", "/abuse/signals", token=ADMIN)
step("operator sees the abuse detector's state", st == 200 and "signals" in ab, f"enabled={ab.get('enabled')}")
# spend the whole credit: a deployer-side adjustment is the honest way to do it here (a top-up or adjustment runs the credit check at once)
call("POST", f"/accounts/{NAME}/credit", {"usd": -0.6, "note": "journey: simulate spend"}, ADMIN)
time.sleep(1)
st, e = call("POST", "/sandboxes", {"mem": 128}, key)
step("at zero credit the account is suspended with the reason members can read", st == 403 and e.get("reason") == "out of credit", f"{st} {e.get('error')} / {e.get('reason')}")
call("POST", f"/accounts/{NAME}/suspend", {"reason": "journey: abuse drill"}, ADMIN)
st, e = call("GET", f"/sandboxes/{sid}/files?path=%2Fetc%2Fhostname", token=key)
step("a suspended account cannot wake its sandbox by reading a file", st == 403 and e.get("error") == "account suspended", f"{st}")
st, info = call("GET", f"/sandboxes/{sid}", token=key)
step("the sandbox was paused and can still be inspected", st == 200 and info.get("status") == "paused", str(info.get("status")))
st, _ = call("POST", f"/accounts/{NAME}/unsuspend", None, ADMIN)
call("POST", f"/accounts/{NAME}/credit", {"usd": 5}, ADMIN)
st, e = call("POST", "/sandboxes", {"mem": 128}, key)
step("after a top-up and unsuspend the account creates again", st == 201, f"{st}")
if st == 201: call("DELETE", f"/sandboxes/{e['id']}", token=key)
st, bd = call("GET", f"/metering/projects/{NAME}/breakdown?by=sandbox", token=owner)
step("the usage breakdown answers", st == 200 and "rows" in bd, f"{len(bd.get('rows', []))} rows")
st, stat = call("GET", "/status")
step("the public status is reachable without a token", st in (200, 503) and "components" in stat, stat.get("status", ""))
st, out = call("DELETE", f"/accounts/{NAME}?force=1", token=ADMIN)
step("offboarding: the account and its sandboxes are deleted", st == 200 and out.get("deleted") is True, f"{st} sandboxes_deleted={out.get('sandboxes_deleted')}")
step("the customer's tokens stop working", call("GET", "/me", token=owner)[0] == 401 and call("GET", "/me", token=key)[0] == 401)
print("JOURNEY FAILED: " + "; ".join(FAILED) if FAILED else "JOURNEY OK")
sys.exit(1 if FAILED else 0)
