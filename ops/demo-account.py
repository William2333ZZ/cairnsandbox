#!/usr/bin/env python3
"""A throw-away demo account with some activity, for screenshots and demos (D68): prints {"account", "owner_token", "key_secret", "sandbox"} as JSON.
`demo-account.py --cleanup NAME` deletes it again (force: its sandboxes go too). CAIRN_URL / CAIRN_TOKEN (the deployer's) come from the environment and are never printed.

    CAIRN_URL=http://fc-cp:8700 CAIRN_TOKEN=... python3 ops/demo-account.py            # create
    CAIRN_URL=http://fc-cp:8700 CAIRN_TOKEN=... python3 ops/demo-account.py --cleanup demo-123456"""
import json, os, sys, time, urllib.error, urllib.request

URL = os.environ.get("CAIRN_URL", "http://127.0.0.1:18700").rstrip("/"); ADMIN = os.environ["CAIRN_TOKEN"]


def call(method, path, body=None, token=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read(); return e.code, (json.loads(raw) if raw[:1] == b"{" else {"raw": raw[:200].decode(errors="replace")})


if len(sys.argv) > 2 and sys.argv[1] == "--cleanup":
    print(call("DELETE", f"/accounts/{sys.argv[2]}?force=1", token=ADMIN)); sys.exit(0)
name = "demo-" + str(int(time.time()))[-6:]
_, inv = call("POST", "/invites", {"note": "demo", "credit_usd": 5, "max_sandboxes": 8, "daily_usd": 5, "ttl_s": 900}, ADMIN)
_, su = call("POST", "/signup", {"code": inv["code"], "account": name, "user": name + "-owner", "name": "Demo Owner"})
owner = su["token"]
call("POST", f"/projects/{name}/members", {"user": name + "-dev", "name": "Demo Dev"}, owner)
_, k = call("POST", f"/projects/{name}/keys", {"name": "ci-pipeline", "role": "member", "ttl_s": 30 * 86400}, owner)
call("POST", f"/projects/{name}/keys", {"name": "grafana-readonly", "role": "viewer"}, owner)
_, sb1 = call("POST", "/sandboxes", {"mem": 256, "network": {"mode": "allowlist", "allow_hosts": ["pypi.org"]}, "metadata": {"team": "red", "job": "etl"}, "start_cmd": "python3 -m http.server 8080 --bind 0.0.0.0"}, owner)
_, sb2 = call("POST", "/sandboxes", {"mem": 128, "class": "ephemeral", "metadata": {"team": "blue"}}, owner)
call("POST", f"/sandboxes/{sb1['id']}/exec", {"cmd": "echo demo > /home/agent/notes.txt; sleep 1"}, owner)
call("POST", f"/sandboxes/{sb1['id']}/checkpoints", {"name": "before-etl", "note": "clean state"}, owner)
call("PUT", f"/sandboxes/{sb1['id']}/network", {"mode": "allowlist", "allow_hosts": ["pypi.org", "github.com"]}, owner)   # a member-style relaxation waits for approval when asked by a member; the owner's applies
print(json.dumps({"account": name, "owner_token": owner, "key_secret": k.get("secret"), "sandbox": sb1["id"], "sandbox2": sb2["id"]}))
