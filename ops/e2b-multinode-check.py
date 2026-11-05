#!/usr/bin/env python3
"""E2B data plane across several nodes (needs a 2+ node stack, e.g. deploy/multinode.sh up 2) and across a node crash.

  phase a: create 4 sandboxes, run commands and file operations in each through the E2B surface, check that they landed on more than one node.
  (you then stop the node that holds the printed sandbox: docker kill cairn-multi-fc-node-2-1)
  phase b: POST /nodes/<n>/evacuate on the native API, then use the evacuated sandbox through the E2B SDK again.

Env: E2B_API_URL, E2B_SANDBOX_URL, E2B_API_KEY (the E2B port), CAIRN_URL, CAIRN_TOKEN (the native port). State between phases: --state file.
"""
import argparse, json, os, sys, time, urllib.request
from e2b import Sandbox


def native(method, path, body=None):
    req = urllib.request.Request(os.environ["CAIRN_URL"].rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ['CAIRN_TOKEN']}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=600))


ap = argparse.ArgumentParser()
ap.add_argument("phase", choices=["a", "b"])
ap.add_argument("--state", default="e2b-multinode.state.json")
a = ap.parse_args()

if a.phase == "a":
    placed = {}
    for i in range(4):
        sb = Sandbox.create(timeout=900)
        node = native("GET", f"/sandboxes/{sb.sandbox_id}")["node"]
        sb.files.write(f"/tmp/e2b/n{i}.txt", f"hello from {node}")
        assert sb.commands.run(f"cat /tmp/e2b/n{i}.txt").stdout == f"hello from {node}"
        assert [e.name for e in sb.files.list("/tmp/e2b")] == [f"n{i}.txt"]
        placed[sb.sandbox_id] = node
        print(f"ok  {sb.sandbox_id} on {node}: create, write, run, list")
    nodes = sorted(set(placed.values()))
    print("nodes used:", nodes)
    assert len(nodes) >= 2, "all sandboxes landed on one node: this run says nothing about routing"
    victim = next(s for s, n in placed.items() if n != "fc-node")
    sb = Sandbox.connect(victim)
    sb.files.write("/tmp/e2b/survivor.txt", "I was on " + placed[victim])
    sb.pause()                                   # a durable sandbox publishes its bundle when it pauses: that is what evacuation restores
    json.dump({"victim": victim, "node": placed[victim], "all": placed}, open(a.state, "w"))
    print(f"paused {victim} on {placed[victim]}; stop that node now, then run phase b")
else:
    st = json.load(open(a.state))
    r = native("POST", f"/nodes/{st['node']}/evacuate", {})
    print("evacuate:", json.dumps(r)[:300])
    now = native("GET", f"/sandboxes/{st['victim']}")["node"]
    assert now != st["node"], f"still on the dead node {now}"
    sb = Sandbox.connect(st["victim"])
    assert sb.files.read("/tmp/e2b/survivor.txt") == "I was on " + st["node"]
    assert sb.commands.run("echo alive-on-" + now).stdout == "alive-on-" + now + "\n"
    print(f"ok  {st['victim']} moved {st['node']} -> {now}; file and command work through the E2B SDK")
