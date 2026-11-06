# Python SDK Reference

Source: [client.py](../../../sdk/python/cairnsandbox/client.py) (Client and the top-level collections), [sandbox.py](../../../sdk/python/cairnsandbox/sandbox.py) (a single sandbox). For the HTTP routes see [api/README.md](api/README.md); the route each method forwards to is given in parentheses, and by topic see [sandboxes](api/sandboxes.md), [state](api/state.md), [images](api/images.md), [network](api/network.md), [metering](api/metering.md), [tenancy](api/tenancy.md), [ops](api/ops.md).

## Installation and configuration

```
pip install -e sdk/python      # zero dependencies (standard library only), Python >= 3.9
```

| Environment variable | Purpose | Default |
|---|---|---|
| `CAIRN_URL` | Control plane address | `http://127.0.0.1:18700` |
| `CAIRN_TOKEN` | Bearer token | empty (no Authorization sent) |
| `CAIRN_PROJECT` | Project, sent as the `X-Project` header | empty (use the token's default project) |

Constructor arguments take precedence over environment variables. Return values are always the server's JSON (dict / list), not wrapped in models; `Sandbox` and `CommandResult` are the exceptions.

```python
from cairnsandbox import Client
c = Client()                                   # reads environment variables
c = Client("http://cp:18700", token="…", project="acme", timeout=600)
```

## Error classes

| Class | Status code | Meaning |
|---|---|---|
| `ApiError` | other 4xx/5xx; `status=0` on transport failure | Base class. Attributes `.status .message .body .method .path` |
| `NotFound` | 404 | Sandbox / volume / checkpoint etc. does not exist |
| `Forbidden` | 403 | Token lacks permission |
| `NoCapacity` | 429 | No node has headroom, volume is in use, sandbox-count quota |
| `QuotaExceeded` | 402 | Project's daily budget exhausted |
| `NeedsApproval` | 202 | Relaxing egress is waiting for the project owner's approval; `.request` is the pending request |

`message` is taken from the `error` field of the response JSON. `NeedsApproval` is triggered by a 202 only in requests that are neither `raw` nor `stream`. `CommandResult.check()` raises `RuntimeError`, not `ApiError`.

## Client

`Client(url=None, token=None, project=None, timeout=600)`

Sub-objects: `sandboxes volumes templates layers builds billing projects network`.

| Method | Description |
|---|---|
| `request(method, path, body=None, raw=False, headers=None, timeout=None, stream=False)` | Low level. `body` is a dict (JSON) or bytes (octet-stream); `raw=True` returns bytes; `stream=True` returns the unread response object. `get/post/put/delete(path, ...)` are shorthands for it |
| `me()` | `GET /me` → `{user, account, projects…}` |
| `health()` | `GET /health` |
| `nodes()` | `GET /nodes` → list of nodes (including `name`, `alive`) |

### Client.sandboxes

| Method | Returns | Description |
|---|---|---|
| `create(template=None, mem=None, vcpu=None, disk=None, cls=None, network=None, env=None, volumes=None, layers=None, template_key=None, ttl_idle_s=None, ttl_max_s=None, retain_paused_s=None, priority=None, qos=None, boot=None, durable=None, node=None, metadata=None, **extra)` | `Sandbox` | `POST /sandboxes`. Parameters whose value is `None` are not sent, leaving the server defaults. `cls` is sent as `class`: `persistent`/`ephemeral`; `priority`: `interactive`/`batch`/`background`; `volumes`: list of ids or `{id, mount, ro}`; `network`: `{mode, allow_hosts, allow_direct, rules, limits}`; `**extra` is passed through as is |
| `get(sid)` | `Sandbox` | `GET /sandboxes/{id}`; 404 → `NotFound` |
| `list()` | `[Sandbox]` | `GET /sandboxes` |
| `import_bundle(bundle, node=None)` | `Sandbox` | `POST /sandboxes/import`. When a node is lost and nobody has evacuated it, re-lands a durable sandbox from the last published bundle |

### Client.volumes

| Method | Description |
|---|---|
| `create(name, size_mib=1024, project=None, node=None)` | `POST /volumes` → volume dict |
| `from_archive(name, tar_bytes, size_mib=None, node=None)` | `POST /volumes/from-archive?name=…`, the request body is the tar bytes; builds a content volume directly without mounting it |
| `list()` | `GET /volumes` → list |
| `get(vid)` / `delete(vid)` | `GET` / `DELETE /volumes/{id}` |

### Client.templates / layers / builds

| Method | Description |
|---|---|
| `templates.list()` | `GET /templates` → list |
| `templates.everywhere(spec)` | `POST /templates/everywhere`, spreads a template to all nodes; `spec` is a dict |
| `layers.list()` | `GET /layers` → list |
| `layers.everywhere(lid)` | `POST /layers/{id}/everywhere` |
| `builds.from_oci(image, name=None, node=None)` | `POST /builds {kind:"oci"}`, OCI image → layer |
| `builds.from_archive(tar_bytes, name, node=None)` | `POST /builds/archive?name=…`, tar → layer |
| `builds.commit(sandbox_id, name, note=None)` | `POST /builds {kind:"commit"}`, running sandbox → named template |
| `builds.get(bid)` / `builds.list()` | `GET /builds/{id}`, `GET /builds` |
| `builds.wait(bid, timeout=1800, every=2.0)` | Polls until `status` is `done`/`failed`; on timeout it still returns the last dict (does not raise, so check `status` yourself) |

### Client.billing

`project` defaults to `client.project`, then to `"default"`.

| Method | Returns | Route |
|---|---|---|
| `today(project=None)` | float, today's spend in USD | `GET /metering/projects/{p}/today` → `spent_usd` |
| `statement(project=None, since=None, until=None)` | dict | `GET …/statement?from=&to=` |
| `statement_csv(project=None, since=None, until=None)` | str (CSV) | `GET …/statement?format=csv` |
| `settle(since, until, project=None)` | dict | `POST …/statements {from,to}`, issues the statement and persists it |
| `statements(project=None)` | list | `GET …/statements` |
| `prices()` | dict | `GET /metering/prices` |
| `sandbox_usage(sid)` | list | `GET /metering/sandboxes/{id}` → `usage` |

### Client.projects

| Method | Description |
|---|---|
| `list(account=None)` | `GET /accounts/{account}/projects`; account defaults to `me()["account"]`, then to `default` |
| `quota(project=None)` | `GET /projects/{p}/quota` |
| `set_quota(project=None, **q)` | `PUT /projects/{p}/quota`, `q` holds the quota fields |
| `members(project=None)` | `GET …/members` → list |
| `add_member(user, role="member", name=None, project=None)` | `POST …/members` |
| `default_network(project=None)` | `GET …/network` → the project's default egress policy |
| `set_default_network(network, project=None)` | `PUT …/network` |

### Client.network

Relaxation requests waiting for the owner to act on (`/network/requests`).

| Method | Description |
|---|---|
| `requests(all=False)` | `GET /network/requests` (`all=True` adds `?all=1`) → list |
| `approve(rid)` / `deny(rid)` | `POST /network/requests/{id}/approve|deny` → the updated request dict |

## Sandbox

Returned by `Client.sandboxes.*`. `.info` is the most recent server response (a dict), and `.id .status .node` are read-only properties of it; `refresh()` fetches it again. Supports `with`; on exit it calls `kill()` and swallows all exceptions.

| Method | Description |
|---|---|
| `refresh()` | `GET /sandboxes/{id}` → new `info` (dict) |
| `pause()` / `resume()` | `POST …/pause|resume`, updates `info`, returns self |
| `kill()` (alias `delete`) | `DELETE /sandboxes/{id}` → `{deleted: id}` |
| `set_timeout(ttl_idle_s=None, ttl_max_s=None, retain_paused_s=None)` | `POST …/ttl`, returns self. `retain_paused_s=0` = keep until deleted |
| `fork(n=1, checkpoint=None, cls="ephemeral", ttl_idle_s=None, ttl_max_s=None, qos=None)` | `POST …/fork` → `[Sandbox]` (copy-on-write; the parent sandbox keeps running). Without `checkpoint`, forks from this moment |
| `pack(name)` | `POST …/pack {name}` → dict |
| `commit(name, note=None)` | Equivalent to `client.builds.commit(self.id, …)`; returns a build dict, use `builds.wait` to wait for completion |
| `migrate(to=None)` | `POST …/migrate`; `to` is the target node, by default chosen by the scheduler |
| `diff(cwd="/work/repo", base="main")` | `GET …/diff?cwd=&base=` → dict |
| `usage()` | `client.billing.sandbox_usage(id)` |
| `budget(max_usd=None, max_tokens=None)` | `PUT …/budget` → `budget` dict |
| `ticket(ticket)` | `PUT …/ticket {ticket}` → `ticket` |
| `inbox(text, kind="note", actor=None, thread=None)` | `POST …/inbox` → dict; delivers a message to the harness inside the sandbox |
| `events(since=0, max=None)` | `GET …/events?since=&max=` → `(events, last_seq)` |
| `wait_for_report(timeout=3600, every=2.0, since=0, include_progress=False)` | Polls `events` and returns the first event with `type=="report"` and `data.state` of ready / waiting / blocked / failed (`{seq, ts, type, data}`); with `include_progress=True` progress events are returned too; returns `None` on timeout |

### sb.commands

| Method | Description |
|---|---|
| `run(cmd, cwd=None, env=None, timeout=None, user=None, stdin=None, landlock=None)` | `POST …/exec` (`bash -c`). `user`: `agent`/`root`; `landlock`: `True` (default restrictions) or `{ro, rw, connect_ports}`. Returns `CommandResult`. HTTP timeout = `(timeout or 600) + 30` |
| `stream(cmd, cwd=None, env=None, timeout=None, user=None)` | `POST …/exec/stream` (NDJSON). A generator that yields `("stdout"\|"stderr", text)`, with the last item being `("done", CommandResult)`. `stdin` and `landlock` are not supported |

`CommandResult`: `.exit` (int) `.stdout .stderr .timed_out` (bool, taken from the `timeout` field) `.elapsed_s .raw` (the original dict) `.check()` (raises `RuntimeError` when `exit != 0`, returns self). A command timeout does not raise; look at `timed_out`.

### sb.files

| Method | Description |
|---|---|
| `write(path, data)` | `PUT …/files?path=`; `str` is encoded as UTF-8; returns the number of bytes |
| `read(path)` | `GET …/files?path=` → bytes; `NotFound` if it does not exist |
| `read_text(path, encoding="utf-8")` | str |
| `list(path="/home/agent")` | List of names in the directory, subdirectories with a trailing `/`. Implemented by running `ls -1Ap` inside the sandbox, not a dedicated route |
| `exists(path)` | bool (`test -e`) |
| `remove(path)` | `rm -rf --`; raises `RuntimeError` on failure |
| `mkdir(path)` | `mkdir -p --` |

The last four are implemented via `commands.run`, so they go through `exec` (counted as commands, and wake the sandbox if it is paused).

### sb.pty

`open(user=None, pty=True)` → `Session` (`POST …/sessions`). `pty=False` gives a long-lived shell without a TTY.

`Session`: `.id .pty`; `exec(cmd, timeout=60)` → `CommandResult` (`POST …/sessions/{sid}/exec`, state is kept across calls); `write(data)` (str/bytes, raw byte stream); `read(wait=0.5)` → bytes (waits at most `wait` seconds); `close()` (`DELETE …/sessions/{sid}`). Supports `with`.

### sb.ports

| Method | Description |
|---|---|
| `url(port, path="/")` | `{client.url}/sandboxes/{id}/ports/{port}{path}`, pure string concatenation, sends no request |
| `get(port, path="/", headers=None)` | `(status, headers, body)`; adds the Bearer automatically; HTTP error statuses are returned rather than raised |
| `websocket(port, path="/", headers=None)` | A raw socket that has completed the handshake (101); you build the frames yourself; raises `RuntimeError` on non-101. Carries `X-Project` (when the client has a project set), and the handshake key is random each time |

### sb.checkpoints

| Method | Description |
|---|---|
| `create(name=None, note=None)` | `POST …/checkpoints` → `{name, chain_len, paused_s…}` |
| `list()` | `GET …/checkpoints` → list |
| `rollback(name)` | `POST …/rollback {checkpoint}`, rolls back in place, updates `sb.info` and returns it; `NotFound` if there is no such checkpoint |
| `drop(name)` | `DELETE …/checkpoints/{name}` |

### sb.network

| Method | Description |
|---|---|
| `get()` | After `refresh()`, returns `info["network"]` |
| `set(**policy)` | `PUT …/network`, fields `mode allow_hosts allow_direct rules limits`, returns the `network` dict. Tightening takes effect immediately; a member relaxing it → raises `NeedsApproval` |
| `decisions()` | `GET …/egress` → `decisions` list (egress verdict records) |

## Mapping to the E2B SDK

**If you already use the official E2B SDK, you do not need to switch:** point `E2B_API_URL` and `E2B_SANDBOX_URL` at the control plane's E2B port (18710) and it works; for coverage and measured results see [api/e2b.md](api/e2b.md). Below is how **our own SDK** `cairnsandbox` maps to the E2B SDK in call shape (it uses the main port 18700 and adds checkpoints, rollback, budgets and more).

| E2B | This SDK |
|---|---|
| `Sandbox.create(template, timeout, envs, metadata)` | `client.sandboxes.create(template=, env=, metadata=, ttl_idle_s=)`; E2B's `timeout` ≈ `ttl_idle_s`; the environment variable parameter is called `env`, not `envs` |
| `Sandbox.connect(id)` | `client.sandboxes.get(sid)` |
| `Sandbox.list()` | `client.sandboxes.list()` |
| `sbx.commands.run(cmd, cwd, envs, timeout, user)` | `sb.commands.run(cmd, cwd=, env=, timeout=, user=)`, returns a `CommandResult` synchronously (`exit stdout stderr`); `stream()` corresponds to the `on_stdout/on_stderr` callbacks |
| `sbx.files.read / write` | `sb.files.read` (bytes) / `read_text` / `write`; plus `list exists remove mkdir` |
| `sbx.pty.create(...)` | `sb.pty.open(user=, pty=)` → `Session` (`write` / `read`) |
| `sbx.set_timeout(n)` | `sb.set_timeout(ttl_idle_s=n)` (also `ttl_max_s`, `retain_paused_s`) |
| `sbx.kill()` | `sb.kill()` (alias `delete`) |
| `sbx.pause()` / `Sandbox.resume` | `sb.pause()` / `sb.resume()` |
| `sbx.get_host(port)` | `sb.ports.url(port)` (forwarded through the control plane, needs a Bearer) |

What E2B has but this SDK's code does not (confirmed absent from client.py / sandbox.py): the async client (`AsyncSandbox`); `commands.run(background=True)` / `commands.list` / `commands.kill` / `commands.connect` / `send_stdin`; callback parameters such as `on_stdout`; `pty.resize`; `files.watch_dir` / `files.get_info` / batch `write_files`, and `files.list` returning metadata (here it returns only names); `is_running` / `get_info` / `get_metrics`; `Sandbox.create` as a class method (here it goes through `Client`); the `Template` build DSL (here we use `builds`). The official E2B SDK can only reach this platform through the E2B REST compatibility surface on the second port ([api/e2b.md](api/e2b.md)); its data plane (envd) is not provided, so use this SDK for commands and files.

## What we add

| Capability | Entry point |
|---|---|
| Checkpoints / in-place rollback / fork from a checkpoint | `sb.checkpoints.*`, `sb.fork(checkpoint=)` |
| Budget | `sb.budget(max_usd=, max_tokens=)` |
| Ticket | `sb.ticket(ticket)` |
| Inbox | `sb.inbox(text, kind=, actor=, thread=)` |
| Event log and waiting for a report | `sb.events(since=)`, `sb.wait_for_report()` |
| Egress policy and approval | `sb.network.set()` + `NeedsApproval`; `client.network.requests/approve/deny`; `sb.network.decisions()` |
| Volumes | `client.volumes.*`, mounted via `create(volumes=[…])` |
| Builds | `client.builds.*`, `sb.commit()`, `templates/layers.everywhere` |
| Billing | `client.billing.*`, `sb.usage()` |
| Projects and quotas | `client.projects.*` |
| Migrate / pack / diff / re-land | `sb.migrate()`, `sb.pack()`, `sb.diff()`, `sandboxes.import_bundle()` |

## Examples

**Context manager** (deleted on exit)

```python
with c.sandboxes.create(mem=512, env={"MODE": "dev"}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
```

**Streaming output**

```python
for ev, data in sb.commands.stream("for i in 1 2 3; do echo $i; sleep 1; done"):
    if ev == "done": print("exit", data.exit)
    else: print(ev, data, end="")
```

**PTY / sessions**

```python
with sb.pty.open(pty=False) as sh:             # long-lived shell without a TTY; state is kept across exec calls
    sh.exec("cd /tmp && export X=1")
    print(sh.exec("pwd; echo $X").stdout)
with sb.pty.open() as t:                       # real PTY, raw bytes
    t.write("ls\n"); print(t.read(0.5).decode())
```

**Fork from a checkpoint**

```python
sb.checkpoints.create("clean")
sb.commands.run("rm -rf /home/agent/*")
sb.checkpoints.rollback("clean")                       # go back in place
a, b = sb.fork(n=2, checkpoint="clean", ttl_idle_s=120)  # or fork two copies
```

**Build a volume from a tar and mount it**

```python
import io, tarfile
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t: t.add("harness/", arcname=".")
v = c.volumes.from_archive("harness", buf.getvalue(), size_mib=256)
sb = c.sandboxes.create(volumes=[{"id": v["id"], "mount": "/opt/harness", "ro": True}])
```

**Settle a statement and export CSV**

```python
print("today", c.billing.today())
open("statement.csv", "w").write(c.billing.statement_csv(since=t0, until=t1))
c.billing.settle(t0, t1)                               # issues the statement and persists it; c.billing.statements() lists history
```

**A relaxation that gets a 202**

```python
from cairnsandbox import NeedsApproval
try:
    sb.network.set(mode="audited", allow_hosts=["pypi.org"])
except NeedsApproval as e:
    print("waiting for owner approval:", e.request["id"])
# on the owner's client:
for r in c.network.requests():
    if r["status"] == "pending": c.network.approve(r["id"])   # or deny
```

**Budget + wait for a report**

```python
sb.budget(max_usd=2.0); sb.ticket("fix-123"); sb.inbox("please fix test_x")
rep = sb.wait_for_report(timeout=1800)                 # None on timeout
```

## The `aios` command line

After `pip install -e sdk/python` there is an `aios` command (D63; zero dependencies, same environment variables as the SDK, or use `--url / --token / --project`; `--json` prints raw JSON).

```
cairn whoami                                   cairn status          # public status, no token needed; exit code 2 on 503
cairn ls                                       cairn usage           # spend today and this period
cairn create --mem 512 --allow pypi.org        # prints the sandbox id
cairn exec sb-1a2b -- python3 -V               # the exit code is the command's exit code
cairn pause|resume|rm sb-1a2b
cairn cp ./a.txt sb-1a2b:/home/agent/a.txt     cairn cp sb-1a2b:/home/agent/out.txt ./
cairn checkpoint sb-1a2b before                cairn rollback sb-1a2b before
cairn keys ls | create ci --role viewer --days 30 | revoke key-…
cairn build py-data --from python:3.12-slim --run 'pip install pandas' --allow pypi.org   # build a pre-warmed template in one step (see the tutorial)
```
Errors (404, 403, 402, 429, …) are printed as a single line `cairn: <status code>: <message>` and exit with 1, without a traceback.
