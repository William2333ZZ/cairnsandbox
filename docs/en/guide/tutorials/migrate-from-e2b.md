# Migrate from E2B

We are not a complete drop-in replacement for E2B; instead two layers coexist ([decisions D40, D43](../../../decisions.md) (Chinese)):

- **The control-plane half is usable directly with E2B's SDK**: the second port (default **18710**) speaks E2B's REST, and `X-API-Key` is your token. Create, list, get, pause, resume, kill, change timeout, connect, and list templates all work.
- **The data-plane half is not there**: E2B's SDK sends commands, files, and PTY to the `envd` inside the sandbox (`49983-<id>.<domain>`), and we have no `envd`. For this part, switch to our Python SDK, which goes over 18700.

So the way to migrate is: **code that manages the sandbox lifecycle can stay as it is; code that executes commands and handles files is switched to our SDK.**

## Item-by-item comparison

| In E2B | Usable as is? | Here |
| --- | --- | --- |
| `Sandbox.create(template, timeout, metadata, envs)` | Yes, pointed at 18710 | `POST /sandboxes {templateID, timeout, metadata, envVars}`. `templateID` is our **template name** (hot start) or `base`; `timeout` (seconds) corresponds to the idle TTL (`ttl_idle_s`); `metadata` is recorded on the route and the node can't see it |
| `Sandbox.list()` | Yes | `GET /sandboxes` or `/v2/sandboxes`; members see only their own account's |
| Get one / `Sandbox.connect(id)` | Yes | `GET /sandboxes/<id>`, `POST /sandboxes/<id>/connect` (resumes and returns the description) |
| `pause()` / resume | Yes | `POST /sandboxes/<id>/pause`, `/resume`, 204 |
| `kill()` | Yes | `DELETE /sandboxes/<id>`, 204 |
| `set_timeout()` / refresh | Yes | `POST /sandboxes/<id>/timeout` or `/refreshes {timeout}`, 204 |
| List templates | Yes | `GET /templates` → `[{templateID, aliases, buildID, cpuCount, memoryMB, createdAt, node}]`, a snapshot of the templates on each node |
| `commands.run(...)` | **No** | `sb.commands.run(cmd, cwd=, env=, timeout=, user=)`, synchronously returns `CommandResult(exit, stdout, stderr)`; for streaming use `sb.commands.stream(...)` |
| `files.read` / `files.write` | **No** | `sb.files.read` / `read_text` / `write`; also `list`, `exists`, `remove`, `mkdir` |
| `pty` | **No** | `sb.pty.open()` gives a `Session`, with `write` / `read` byte streams |
| `get_host(port)` (wildcard domain `<port>-<id>.<domain>`) | **No** | `sb.ports.url(port)` gives `http://<control plane>/sandboxes/<id>/ports/<port>/…`, which needs `Authorization: Bearer`; `sb.ports.get(port)` adds it for you; for WebSocket use `sb.ports.websocket(port)` |
| Template build (`e2b template build` / Dockerfile) | **No** | `POST /builds`: an OCI image to a layer, a running sandbox committed as a template; see the [custom template tutorial](custom-template.md); we don't read Dockerfiles |
| Volumes | **No** | `POST /volumes`, attached with `volumes=[…]` when creating a sandbox; see [manual section 6](../manual.md) |
| Async client, background processes and `send_stdin` in `commands`, `files.watch_dir`, etc. | None | This SDK doesn't have them; for background processes start them yourself with something like `commands.run("nohup … &")` |

The `sandboxID` in E2B responses is the same as our sandbox id, and both ports see the same set of sandboxes, so **you can mix them**: create with the E2B SDK and execute with ours.

`envVars` is given at creation and lands in this sandbox's `env`, which is laid underneath the environment of every one of our `commands.run` calls (the request's own `env` is laid on top); the E2B `envd` notion of "the environment at process start" does not exist.

On 18710 the error body is `{code, message}`; on 18700 it is `{error}`, and our SDK maps status codes to `NotFound` (404), `Forbidden` (403), `NoCapacity` (429), `QuotaExceeded` (402), `NeedsApproval` (202).

## Point the E2B SDK at us

Two things: change the API address to `http://<control plane>:18710`, and change the API key to your token (a member token or a deployer token; `Authorization: Bearer` is also accepted). The E2B SDK is generally configured through the environment variables `E2B_API_URL` and `E2B_API_KEY`; that is a setting of E2B's own SDK, and the exact names and whether a custom API address is supported depend on the version you installed. The repository has not done end-to-end testing against any specific version of the E2B SDK; all that is guaranteed is the REST shape in [CONTRACT §1a](../../../../CONTRACT.md). To pick a project, add an `X-Project` header (omitted means your account's default project).

```bash
export E2B_API_URL=http://127.0.0.1:18710
export E2B_API_KEY=<your token>
curl -s -H "X-API-Key: $E2B_API_KEY" $E2B_API_URL/sandboxes     # first verify the port works with curl
```

## Before and after

**Before** (E2B only):

```python
from e2b import Sandbox

sbx = Sandbox.create(template="base", timeout=300, envs={"MODE": "dev"})
sbx.files.write("/home/user/app.py", "print('hi')")
r = sbx.commands.run("python3 /home/user/app.py")
print(r.stdout)
sbx.set_timeout(600)
sbx.kill()
```

**After** (the same task, with execution and files switched to our SDK; the default home directory in file paths is `/home/agent`):

```python
from cairnsandbox import Client

c = Client("http://127.0.0.1:18700", token="…", project="acme")     # or CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
sb = c.sandboxes.create(ttl_idle_s=300, env={"MODE": "dev"})        # E2B's timeout ≈ ttl_idle_s; envs → env
sb.files.write("/home/agent/app.py", "print('hi')")
r = sb.commands.run("python3 /home/agent/app.py")
print(r.stdout)
sb.set_timeout(ttl_idle_s=600)
sb.kill()
```

**Middle ground** (keep using E2B's SDK for the lifecycle and replace only the data plane):

```python
from e2b import Sandbox                  # points at 18710
from cairnsandbox import Client           # points at 18700

sbx = Sandbox.create(template="base", timeout=300)
sb = Client("http://127.0.0.1:18700", token="…").sandboxes.get(sbx.sandbox_id)   # the same id
print(sb.commands.run("uname -a").stdout)
sbx.kill()
```

(Attribute names in the E2B SDK change between versions; `sandbox_id` above depends on the version you installed. If you can't get it, read it from `sandboxID` in `GET /sandboxes`.)

## What you gain after migrating

What E2B doesn't have but is here can be adopted step by step: checkpoints and in-place rollback, forking from a checkpoint, setting budgets and writing tickets for a sandbox ([Budgets, Tickets, and Reports](budget-and-ticket.md)), L7 rules and credential injection in egress policy, project quotas and billing, relaxing via approval. For the full table see ["Correspondence with the E2B SDK" in the SDK reference](../sdk.md) and the [API reference](../api/e2b.md).

## Pre-migration checklist

1. The call sites of `commands.`, `files.`, `pty.`, and `get_host` in your code; all of these must be replaced.
2. Whether every template you use exists on our side (`GET /templates`); for those that don't, redo them with the [custom template tutorial](custom-template.md); the template name is the `templateID`.
3. Steps that depend on the outside network: our sandboxes have none by default, so give `network.allow_hosts` at creation.
4. Change `/home/user` in paths to `/home/agent`.
5. Timeout semantics: `timeout` is the **idle** TTL, not a maximum lifetime; for a maximum lifetime use `ttl_max_s`.
