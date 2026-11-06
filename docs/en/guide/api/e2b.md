# The E2B-shaped port

This page covers the control plane's second port: the REST shape used by the E2B SDK (create / list / pause / resume / fork / snapshot / delete sandboxes, read templates), and the SDK's data plane into the sandbox (envd: commands, files, terminal). Its paths do not conflict with [CONTRACT §1](../../../../CONTRACT.md), so it gets a separate port; the contract is written in §1a, the implementation is `cp/internal/e2b/api.go`, the domain background is in [docs/002](../../../002-domain.md) (Chinese), decision D40. **It is only a skin over the control plane**: the in-sandbox data plane (envd) that the E2B SDK connects to afterwards is not provided by us; see the compatibility matrix below. For calling conventions (Project, roles) see [README](README.md); for the §1 main port see [sandboxes.md](sandboxes.md).

## Port and authentication

| Item | Value |
| --- | --- |
| Port | Environment variable `CP_E2B_PORT`; **it starts only if set**; the convention is `8710` (inside the container), mapped by default to `18710` on the host, i.e. `http://127.0.0.1:18710` |
| Authentication | `X-API-Key: <member token or CP_TOKEN>`; `Authorization: Bearer` is also accepted. The tokens are the same as for §1 (for member tokens see [tenancy.md](tenancy.md)) |
| Project | `X-Project: <id>`, optional, defaults to the caller account's `default`; if it is not yours → 403 |
| Error body | `{"code": 404, "message": "…"}` (different from §1's `{"error"}`) |
| No authentication | `GET /health` → `{"ok": true, "surface": "e2b"}` |
| Default template | `CP_E2B_DEFAULT_TEMPLATE`: used when the SDK does not specify a template (or writes `base`); if not configured, the base template is cold-started |

To point the E2B SDK at it: set both `E2B_API_URL` and `E2B_SANDBOX_URL` to `http://<host>:18710`, and set `E2B_API_KEY` to our token (see the "Data plane" section below for the complete wiring, and the compatibility matrix for measured results).

## Routes

What is listed below is everything (`/v2/…` is synonymous with the v1 path of the same name); any other path → `404 {"code":404,"message":"no route"}`. A sandbox that does not exist or does not belong to your account → `404 sandbox not found`.

### POST /sandboxes · POST /v2/sandboxes

Caller. Request:
```json
{"templateID?": "py312", "template?": "py312", "timeout?": 600, "metadata?": {"k": "v"}, "envVars?": {"A": "1"}}
```
Mapped to §1's create:

| E2B field | Ours |
| --- | --- |
| `templateID` (or `template`) | Our **template name**, written as `template_key`, with `boot: snapshot` (warm start); `"base"` or absent → use `CP_E2B_DEFAULT_TEMPLATE` (if not configured, cold-start the base template) |
| `timeout` (seconds) | `ttl_idle_s`; `≤ 0` or absent means not set |
| `metadata` | Recorded on the route (`route meta`), invisible to the node; values are converted to strings |
| `envVars` | `spec.env`: laid underneath the environment of every exec in this sandbox (there is no init process that could hold them) |
| (fixed) | `class: persistent` |

Response **201**:
```json
{"sandboxID": "sb-123", "templateID": "py312", "clientID": "node-a", "alias": "py312", "state": "running",
 "startedAt": "2026-11-03T08:00:00Z", "endAt": "2026-11-03T08:10:00Z", "cpuCount": 2, "memoryMB": 1024, "diskSizeMB": 512,
 "metadata": {"k": "v"}, "project": "web", "envdVersion": "none", "envdAccessToken": ""}
```
`state`: `running` | `paused` | `unknown` (the sandbox is unreachable / failed). `endAt` is computed as `ttl_idle_s` minus the seconds already idle; it is an empty string if no ttl is set. `envdVersion: "none"` means there is no envd.

Errors: `code` is the status code of the creation failure (402 / 429 for quota and capacity, same as [sandboxes.md](sandboxes.md)), and `message` is the reason.

```bash
curl -s -X POST http://127.0.0.1:18710/sandboxes -H "X-API-Key: $TOKEN" -H "X-Project: web" \
  -d '{"templateID":"py312","timeout":600,"metadata":{"job":"42"},"envVars":{"MODE":"dev"}}'
```

### GET /sandboxes · GET /v2/sandboxes

Caller. The response is an **array** (of the objects above); members see only their own account's, admin sees all. Query parameters: `state=running,paused`, `metadata=k=v&k2=v2` (URL-encoded as a whole), `limit` and `nextToken` (the token for the next page is in the response header `x-next-token`; the token is simply the list offset).

### GET /sandboxes/\<id\>

Response: the object above.

### DELETE /sandboxes/\<id\>

Response **204**. On failure, `code` is the node status code and `message: "kill failed"`.

### POST /sandboxes/\<id\>/pause · resume

Response **204** (corresponding to §1's pause / resume).

### POST /sandboxes/\<id\>/connect

Resumes the sandbox first (it is fine if it is already running), then describes it: **200** with the object above.

### POST /sandboxes/\<id\>/timeout · refreshes

Request `{"timeout": 300}` (seconds; `≤ 0` or absent is treated as 300). Response **204**. Corresponds to the node's `POST /sandboxes/<id>/ttl {ttl_idle_s}`, setting only the idle ttl.

### POST /sandboxes/\<id\>/fork · /snapshots · PUT /network · GET /metrics

- `fork` `{"count?": 1, "timeout?": seconds}` → **201** `[{"sandbox": {…}}]`; clones the whole running VM, and the clone becomes a route in the same Project.
- `snapshots` `{"name?": …, "memory?": …}` (both fields are ignored) → **201** `{"snapshotID": "snap-…", "names": ["snap-…"]}`; the snapshot id can be used as a `templateID` to create a new sandbox. `GET /snapshots` lists this Project's, and `DELETE /templates/<snapshotID>` deletes one (**204**; you can only delete your own snapshots, admin can delete any).
- `PUT …/network` `{"allowOut?": […], "denyOut?": […]}` → **204**; only owner / admin can use it, see the compatibility matrix.
- `GET …/metrics` → array `[{timestamp, timestampUnix, cpuCount, cpuUsedPct, memUsed, memTotal, memCache, diskUsed, diskTotal}]`.

### GET /templates

Response array: `[{"templateID": "py312", "aliases": ["py312"], "buildID": "py312", "cpuCount": 2, "memoryMB": 1024, "public": false, "createdAt": "…", "node": "node-a"}]`, one entry for each template snapshot on each live node.

## Data plane: envd (commands, files, terminal in the sandbox)

After creating a sandbox, the E2B SDK connects to envd in the sandbox for commands, files, and PTY. We serve this half on the **same port**, relaying requests via the node to sandboxd inside the sandbox (which has an extra envd listener on vsock, reachable only from the host), so no wildcard domain, TLS, or per-sandbox hostname is needed.

**Wiring** (identical for the official Python and JS SDKs):

```bash
export E2B_API_URL=http://<host>:18710          # control plane (the routes above)
export E2B_SANDBOX_URL=http://<host>:18710      # same address: all of the SDK's requests to sandboxes go here, distinguished by the E2b-Sandbox-Id header
export E2B_API_KEY=<member token or CP_TOKEN>
```

```python
from e2b import Sandbox
sbx = Sandbox.create(timeout=300)
print(sbx.commands.run("echo hi").stdout)
```

- **Protocol**: Connect's JSON encoding (the official SDKs default to JSON, not binary protobuf): `application/json` for unary calls and `application/connect+json` for server streams. Over plaintext `http://` the SDK speaks HTTP/1.1 (even though the SDK's `http_version` defaults to `"2"`), and we implement only HTTP/1.1.
- **Authentication**: the `envdAccessToken` in the create response is a token bound to **this one sandbox** (`HMAC(CP_ENVD_SECRET or derived from CP_TOKEN, "envd|"+sandbox id)`, shared across replicas, not persisted to disk); the SDK sends it as `X-Access-Token`. The token of another sandbox cannot open this one (401); a leaked token affects only one sandbox. The token appears only in the responses of **create, connect, and fork** (which require write permission); `envdAccessToken` in `GET /sandboxes`, `/v2/sandboxes`, and `/sandboxes/<id>` is always empty, so a read-only key can list sandboxes but cannot obtain a token that can execute commands. If the sandbox does not exist or has been killed → **502** with a message containing `was not found` (the SDK's `is_running()` and `SandboxNotRunningException` rely on it). When the account is suspended, write-type requests are rejected, so that the relay does not resume a paused sandbox.
- **Audit**: the control plane writes an audit entry (`op: envd`) for every data-plane request; `Start` carries the command line (`cmd`), `cwd`, `user`, `pty`, `tag`, and file requests carry the path; the node writes another `op: envd` entry. All of these are in `GET /audit`.
- **User**: the SDK's `user=` is carried to the sandbox via `Authorization: Basic`; when not specified, `agent` is used (not root, and not E2B's `user`; if a request names a `user` that the sandbox does not have as an account, it is treated as `agent`). `root` must be written explicitly.
- **Environment variables**: the `envVars` from creation are likewise visible to processes started by envd (the node writes them into the sandbox's `/run/sandboxd/env.json` at create and resume).
- **Requirement**: the sandbox's base image contains a sandboxd with envd (rebuild `recipes/base-debian-13`; snapshots of existing templates contain the old sandboxd, so templates must be rebuilt). Data-plane requests to a sandbox with an old image get 503, with a message saying the sandbox has no envd.
- **`envdVersion`**: reports `0.5.7`. The SDK decides its behavior by it (`e2b/envd/versions.py`): 0.5.7 is the highest version we fully implement (supports octet-stream upload, stdin, and the default user; does not include file metadata and watch entries).

**Processes survive pause and rollback**: processes live in the VM's memory, so after pause, checkpoint, and rollback they are still in `commands.list()`, and `commands.connect(pid)` can reattach to the same process and keep receiving output (E2B does not have this; the conformance `cairn` group covers it).

## Compatibility matrix (measured with the official SDKs)

**What was tested**: the official `e2b` Python SDK (36 cases) and npm `e2b` (20 cases) against the same control plane. **Version matrix** (2026-11-05, `ops/e2b-matrix.sh`, one separate environment per version, [results](../../../research/e2b-conformance/matrix-2026-11-05.json)): Python 2.52.1, 2.55.1, 2.57.0, 2.60.0, 2.62.1, 2.63.0 and npm 2.52.1, 2.55.0, 2.57.0, 2.60.0, 2.62.2, 2.63.0 **all pass**; **1.x is not supported** (Python 1.11.1 and npm 1.13.2 do not recognize a custom API address and can only connect to e2b.app, and the suite refuses to send requests to them). The remaining notes apply to the scripts, which are [ops/e2b-conformance.py](../../../../ops/e2b-conformance.py) and [ops/e2b-conformance.mjs](../../../../ops/e2b-conformance.mjs); the result files are in [docs/research/e2b-conformance/](../../../research/e2b-conformance/). They were run on 2026-11-05 on a Linux machine (Firecracker, jailer on), first with 1 node and in the last round with **2 nodes**, and **all passed**. **This does not mean other SDK versions, other languages (Go, etc.), or other cases also pass**; an SDK upgrade may change request shapes, so rerun after upgrading.

Legend: **Works** = the conformance cases pass; **Implemented, untested** = the code exists but there is no case; **Partial** = present but with different semantics; **None** = not provided.

### Control plane

| E2B capability | Status | Notes |
| --- | --- | --- |
| Create (`POST /sandboxes`, `/v2/sandboxes`): `templateID`, `timeout`, `metadata`, `envVars` | Works | `timeout` is the **idle** ttl, not E2B's absolute end time; `templateID` is our template name or a snapshot id, and `base` or absent goes to `CP_E2B_DEFAULT_TEMPLATE` (if not configured, cold-start the base template) |
| Create: `allow_internet_access`, `network.allowOut/denyOut` | Implemented, untested (these create-time fields) | `true` → egress `audited` (allow all and record); `false` or only `denyOut` → `closed`; with `allowOut` → `allowlist` (IP/CIDR go into `allow_direct`). When absent, the Project's default policy is used (**not** E2B's "internet open by default") |
| Create: `autoPause`, `volumeMounts`, `mcp`, `secure`, `iam` | None | These fields are ignored (an idle timeout pauses persistent sandboxes anyway) |
| List (`GET /sandboxes`, `/v2/sandboxes`): `state` and `metadata` filters, `limit` and `nextToken` pagination | Works (filters); pagination implemented, untested | The pagination token is the list offset |
| Get, delete, `pause`, `connect` (v1 and v2, `timeout` extends the lifetime), `timeout`/`refreshes` | Works | After `pause`, `Sandbox.connect` resumes, and files are still there |
| `fork` (`POST /sandboxes/<id>/fork`, `count`) | Works | Clones the whole running VM (memory and disk), and the clone becomes a route in the same Project. This is our strong suit |
| Snapshots: `create_snapshot`, `list_snapshots`, `delete_snapshot`, create using a snapshot id as the template | Works | Corresponds to "commit as template" (D37). The snapshot name is `snap-<project tag>-<random>`; a name of your choosing is not accepted (the `names` field contains this id) |
| `update_network` | Works | **Only owner and admin**: a member relaxing the policy needs approval, this port has no approval queue, and members must use the native `PUT /sandboxes/<id>/network` (the 403 says so). E2B's "replace as a whole" semantics are implemented by clearing old rules with `rules: []`; E2B's `rules` (modify request headers per host) are not supported |
| `GET /sandboxes/metrics?sandbox_ids=a,b` (batch) | Works | Returns the latest point for each sandbox; ids that are not yours or do not exist are omitted |
| `get_metrics` | Works | Memory and CPU come from the node's cgroup sampling; `diskUsed` is always 0 |
| Read templates (`GET /templates`) | Partial | Reads only each node's template snapshots; `public` is always false |
| Template build (`Template().from_…().run_cmd(…).build()`, `/v3/templates`) | Partial | See the "Template build" section below |
| Template tags, team and key management, sandbox logs (`/logs`) | None | For members and tokens see [tenancy.md](tenancy.md); for logs see the native `/events` |

### Data plane (envd)

| E2B SDK call | Status | Notes |
| --- | --- | --- |
| `commands.run`: separate stdout / stderr, exit code, non-zero exit raises, `envs`, `cwd`, `user`, command not found = 127 | Works | Output **arrives as it is produced** (the case verified the timing with `sleep`; it is not given all at once after the end) |
| `commands.run(background=True)`, `list`, `kill`, `connect` to reattach | Works | |
| `send_stdin`, `close_stdin` (`stdin=True`) | Works | |
| Command timeout (`timeout=`) only drops the connection; the process and sandbox keep running | Works | Same as E2B: a client timeout does not kill the process |
| `pty.create`, `send_stdin`, `resize`, `kill`, read output | Works | |
| `files.read` / `write` (text, bytes, 20 MiB), `write_files`, a missing file raises NotFound | Works | Uploads support multipart (streamed to disk, not buffered as a whole), octet-stream (streamed), and `gzip=True`. gzip is decompressed in memory in the sandbox, with at most 32 MiB compressed and 64 MiB decompressed; upload larger files without gzip (400 with a hint); the cases include a 40 MiB streaming upload and one gzip round trip |
| `/files/compose` (concatenate several files into one) | Implemented, untested | Covered by unit tests; there is no SDK case (the SDK has no corresponding method) |
| `files.list` (`depth`), `exists`, `get_info`, `make_dir`, `rename`, `remove` | Works | |
| `files.watch_dir` | Works | Relies on polling a directory snapshot every 250 ms, with no inotify: a rename appears as "delete + create", and there is overhead on large directory trees |
| `commands`' `StreamInput` (client streaming), `/init`, `/metrics`, `/freeze` and other envd-internal endpoints | None | `StreamInput` needs HTTP/2 full duplex; the SDK's `send_stdin` uses the unary `SendInput`, so it is unaffected |
| `get_host(port)`: the address of a service started in the sandbox | Partial | When the control plane sets `PREVIEW_DOMAIN` (D55), the `domain` in the create response is that value, `get_host(8000)` yields `8000-<id>.<domain>`, and the control plane rewrites this host to `/sandboxes/<id>/ports/8000/…`; the conformance case verified this with a small service started in the sandbox (accessed with a Bearer header). **Differences from E2B**: E2B is public by default, whereas we require a token (in a browser, carry `?token=` the first time, see [manual.md](../manual.md)); you need to set up wildcard DNS and TLS yourself; when `PREVIEW_DOMAIN` is not set, `domain` is empty, and `get_host` falls back to the SDK's default domain, which is unusable. The native `ANY /sandboxes/<id>/ports/<port>/<path>` (including WebSocket upgrade) is unaffected |

### Multiple nodes and latency (2026-11-05, 2 node containers on the same machine)

- **Multiple nodes**: `ops/e2b-multinode-check.py`: 4 sandboxes were spread across the two nodes by load, and each ran commands and file operations normally through the E2B port; after `docker kill` of the node holding one paused sandbox and then `evacuate`, that sandbox resumed on the other node with its files intact and the SDK's `commands.run` working normally. The sandbox that had never been paused was marked `lost` because it had no bundle (this is evacuate's defined semantics).
- **`commands.run` directly on a paused sandbox** (without `connect` first): the node resumes it transparently; there is a conformance case.
- **Latency of the stack itself** (`ops/e2b-latency-raw.py`, using a standard-library client on the host where the control plane runs, sending the same Connect JSON, excluding the SDK and the network; 1 sandbox for latency, 4 in parallel): `bash -l -c true` from sending `Start` to receiving the end p50 8.6 ms / p95 9.4 ms; first stdout byte 4.4 ms; 1 KiB file write / read p50 4.2 / 3.8 ms; 64 MiB file write 290 MB/s, read 670 MB/s; 4 sandboxes in parallel, 100 commands in total, p50 10.6 ms, p95 21.8 ms, no errors (283 per second). Cold-start create 0.4 s. **These are small-scale single-machine numbers, other people's jobs were also running on the dev machine, and they are not a benchmark against other sandboxes.** The figures measured with the SDK from a laptop through an SSH tunnel (command p50 66 ms) are dominated by the tunnel's 88 ms RTT and have no reference value.

### Template build (`Template.build()`)

The official SDK's template builder works: `Template().from_base_image()` / `from_template(…)` / `from_ubuntu_image()` / `from_debian_image()`, plus `run_cmd`, `copy`, `set_envs`, `set_workdir`, `set_user`, `set_start_cmd(…, ready_cmd)`, then `Template.build(t, "name")` (or `build_in_background`). **The control plane runs the build itself**: it starts a build sandbox, executes the steps (through the node's exec and file endpoints), starts the start command and waits for the ready command to succeed, and finally commits the **running sandbox** as a template (memory plus disk), so sandboxes created from it come up with the start command still running. The conformance case (the `template` group of `ops/e2b-conformance.py`) built a template using `run_cmd`, `copy`, `set_envs`, and `set_start_cmd`, then created a sandbox and verified that the files, the copied-in files, and the environment variables are present and that the start command keeps running.

| | |
|---|---|
| Step types | `RUN` (executed as root by default), `COPY` (the SDK packs the context into a tar and uploads it to `/e2b-uploads/<hash>`, with a link carrying an HMAC and an expiry time, so no API key is needed), `ENV`, `WORKDIR`, `USER`; other types give an error. Convenience methods such as `pip_install` and `apt_install` should expand to `RUN` in the SDK, but **have not been tested individually** |
| Base | `fromImage`: `e2bdev/base` → the node's default template, `ubuntu`/`ubuntu:*` → `ubuntu-24.04`, `debian`/`debian:*` → `debian-13` (**these images are not actually pulled**; the log states which base was used); other images (`from_python_image`, etc.) give an error, and to use another image, first build a layer with the native `POST /builds {kind:\"oci\"}`. `fromTemplate`: our template name |
| Egress | The build sandbox uses the **Project's default egress policy** (default deny-all), so steps that need the network (`pip install`, `apt-get`) fail by default; to allow them, change the Project's default policy. This is intentional: otherwise a member could bypass "relaxing requires approval" through builds |
| Environment variables | `ENV` is written to `/etc/profile.d/99-e2b-template-env.sh` and takes effect for login shells (the SDK's `commands.run` uses `bash -l`); `WORKDIR` only takes effect within the build steps and does not become the sandbox's default directory |
| Names and permissions | The name is the template's global name (starting with a lowercase letter, alphanumerics and `_ . -`, at most 40 characters), without `:tag` (400 if given); a template of the same name belonging to someone else → 409; your own template of the same name is replaced. Delete with `DELETE /templates/<name>` (you can only delete what you built, admin can delete any) |
| Limits | At most 4 concurrent builds per control-plane replica; COPY uploads are kept in a replica-local directory (`CP_E2B_UPLOAD_DIR`), and **with multiple replicas, if the upload and the trigger land on different replicas the file will not be found**; state and logs are in the database and any replica can answer; `CP_E2B_PUBLIC_URL` sets the externally visible address in upload links (by default taken from the request's Host); the committed template lives on the node that built it, and if no node is available, creation reports that there is no such template |

### code-interpreter

The official `e2b_code_interpreter` (`run_code`, contexts) 2.10.5–2.11.9 passes 17 cases; the `code-interpreter-v1` template must be built first; for scope and limits see [../code-interpreter.md](../code-interpreter.md). For requests whose `E2b-Sandbox-Port` is not 49983, the control plane authenticates with the sandbox's traffic token (or the envd token) and forwards to the node's port proxy (streaming).

### Not covered

- There are no tests of E2B SDKs in other languages such as Go or Java.
- Multiple nodes have been verified only between **two node containers on the same machine**, not on two physical machines.
- Concurrency only goes up to 4 sandboxes in parallel; there is no stress test (shared dev machine).
