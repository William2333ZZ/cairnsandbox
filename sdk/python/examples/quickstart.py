#!/usr/bin/env python3
"""Five minutes with the SDK against a real control plane. usage: quickstart.py [cp-url] (token from CAIRN_TOKEN, project from CAIRN_PROJECT).
Prints [OK]/[FAIL] lines and a RESULT line, so CI can run it too."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cairnsandbox import Client, NeedsApproval, NotFound  # noqa: E402

c = Client(sys.argv[1] if len(sys.argv) > 1 else None)
R = {"ok": 0, "fail": 0}
def step(name, cond, detail=""): R["ok" if cond else "fail"] += 1; print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")

print("control plane:", c.url, "| nodes alive:", [n["name"] for n in c.nodes() if n.get("alive")])
t0 = time.time()
with c.sandboxes.create(mem=256, cls="persistent", env={"GREETING": "hello"}, retain_paused_s=600) as sb:
    step("create", sb.status == "running", f"{sb.id} on {sb.node} in {time.time() - t0:.2f}s")
    r = sb.commands.run("echo $GREETING from $(hostname)")
    step("commands.run sees the sandbox env", r.stdout.strip().startswith("hello from "), r.stdout.strip())
    sb.files.write("/home/agent/notes.txt", "first draft\n")
    step("files round trip", sb.files.read_text("/home/agent/notes.txt") == "first draft\n" and "notes.txt" in sb.files.list("/home/agent"))
    chunks = [d for ev, d in sb.commands.stream("for i in 1 2 3; do echo line$i; done") if ev == "stdout"]
    step("commands.stream delivers output as it comes", "".join(chunks).split() == ["line1", "line2", "line3"], str(chunks)[:60])
    ck = sb.checkpoints.create("draft1")
    sb.commands.run("echo second > /home/agent/notes.txt")
    sb.checkpoints.rollback("draft1")
    step("checkpoint + rollback in place", sb.files.read_text("/home/agent/notes.txt") == "first draft\n" and sb.status == "running", f"paused {ck.get('paused_s')}s")
    clones = sb.fork(n=1, checkpoint="draft1", ttl_idle_s=120)
    step("fork from the checkpoint", len(clones) == 1 and clones[0].files.read_text("/home/agent/notes.txt") == "first draft\n", clones[0].id)
    clones[0].kill()
    sb.pause(); step("pause", sb.status == "paused")
    step("a command resumes it transparently", sb.commands.run("echo back").stdout.strip() == "back" and sb.refresh()["status"] == "running")
    with sb.pty.open(pty=False) as sh:
        sh.exec("cd /tmp && export X=1"); step("a session keeps state between calls", sh.exec("pwd; echo $X").stdout.split() == ["/tmp", "1"])
    try:
        sb.network.set(allow_hosts=[]); step("tightening the policy applies at once", True)
    except NeedsApproval as e: step("tightening the policy applies at once", False, str(e))
    step("set_timeout", sb.set_timeout(ttl_idle_s=300).info["ttl_idle_s"] == 300)
    usage = sb.usage(); step("usage is recorded per sandbox", isinstance(usage, list), f"{len(usage)} lines")
try:
    c.sandboxes.get(sb.id); step("the context manager deleted it", False)
except NotFound: step("the context manager deleted it", True)
print(f"today's spend: ${c.billing.today():.4f}")
print(f"RESULT ok={R['ok']} fail={R['fail']}")
