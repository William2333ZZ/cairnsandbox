# cairnsandbox (Python SDK)

The client for the Cairn sandbox cloud's control plane (CONTRACT §1). Standard library only.

```python
from cairnsandbox import Client
c = Client("http://cp:18700", token="…", project="acme")     # or CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
with c.sandboxes.create(mem=512, network={"allow_hosts": ["pypi.org"]}, env={"MODE": "dev"}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
    sb.files.write("/home/agent/app.py", "print('hi')")
    for ev, data in sb.commands.stream("python3 /home/agent/app.py"): print(ev, data)
    sb.checkpoints.create("clean")                     # go back later: sb.checkpoints.rollback("clean"); or branch: sb.fork(checkpoint="clean")
    with sb.pty.open() as sh: sh.write("ls\n"); print(sh.read(0.5))
    print(sb.ports.url(8080))                          # a service inside, through the control plane; sb.ports.websocket(8080) is a byte tunnel
```

What E2B's SDK has, we have in the same shape: `create` / `commands.run` / `files.read|write` / `pty` / `ports` / `pause` / `resume` / `kill` / `set_timeout`.
What is ours: `checkpoints` (take / rollback / fork from), `budget`, `ticket`, `inbox`, `events` / `wait_for_report` (the kernel ABI the harness inside talks to),
`network.set` with L7 rules and limits (a member's loosening raises `NeedsApproval`), `c.volumes`, `c.builds` (OCI image → layer, sandbox → template), `c.billing`, `c.projects`.

Errors are `ApiError` subclasses with `.status`: `NotFound`, `Forbidden`, `NoCapacity` (429), `QuotaExceeded` (402), `NeedsApproval` (202, `.request`).

**MCP**:`python3 -m cairnsandbox.mcp` 是一个 stdio 的 MCP 服务(`claude mcp add aios -- python3 -m cairnsandbox.mcp`),工具:sandbox_create / run / write_file / read_file / checkpoint / rollback / fork / pause / resume / kill / list / info;令牌与地址从环境变量来。

`examples/quickstart.py http://cp:18700` runs the tour against a real control plane and prints `[OK]`/`[FAIL]` lines. `pip install -e sdk/python` installs it.

The E2B REST surface on the second port (CONTRACT §1a) lets E2B's own SDK create and manage sandboxes here; its data plane (envd) is not provided, so commands and files go through this SDK or our HTTP.
