# Sandboxes (Lifecycle and Execution Plane)

This document covers context 2 (sandbox lifecycle) and context 8 (execution plane): create, query, pause / resume, ttl, command execution, sessions (pty), files, fork, checkpoints and rollback, network policy, port exposure, budget / ticket / inbox / events / diff, packing, migration, and export / import. For the contract see [CONTRACT §1](../../../../CONTRACT.md), for the domain design see [docs/002 (Chinese)](../../../002-domain.md), and for the sandbox itself see [docs/003 (Chinese)](../../../003-sandbox.md). Volumes are in [state.md](state.md), templates and builds in [images.md](images.md), credentials and rule syntax in [network.md](network.md), and billing in [metering.md](metering.md).

**Routing convention**: for paths under `/sandboxes/<id>/…` other than `migrate` (admin), the control plane checks ownership and forwards them unchanged to the node hosting the sandbox (body, status code, and chunked responses all pass through). A sandbox of another account is a 404 to you. Node down → 503 (with `durable`), node unknown → 503, forwarding failure → 502. In everything below, "caller" means a member who can see that sandbox (same account) or admin. The `Idempotency-Key` header is passed through to the node.

The main fields of the sandbox info: `id, status(running|paused|…), template, class(persistent|ephemeral), vcpu, mem, ttl_idle_s, ttl_max_s, created, node, durable, boot_mode, layers?, network?, project, priority`, plus `disk_used_mib, egress:{out,in}, checkpoints, volumes, template_snapshot, retain_paused_s, paused_at, env_keys` (`GET` of a single sandbox returns more). For the remaining fields see CONTRACT §1.

## POST /sandboxes

Caller (member or admin). Creates a sandbox; ownership is determined by `X-Project` / `project` / `?project=` (default: the account's default).

Request:
```json
{
  "template": "ubuntu-24.04",
  "class": "persistent",
  "vcpu": 2, "mem": 1024, "disk": 512,
  "ttl_idle_s": 300, "ttl_max_s": 3600,
  "qos": "normal",
  "priority": "interactive",
  "boot": "auto",
  "layers": ["L-xxxxxxxx"],
  "network": {"mode": "allowlist", "allow_hosts": ["pypi.org"], "allow_direct": [], "rules": [], "limits": {"requests_per_min": 600, "max_bytes": 104857600}},
  "retain_paused_s": 0,
  "env": {"NAME": "value"},
  "volumes": ["vol-id", {"id": "vol-id", "mount": "/mnt/data", "ro": false}],
  "metadata": {"k": "v"},
  "node": "node-a",
  "template_key": "my-template",
  "project": "web"
}
```
All fields are optional.

| Field | Meaning | Default / constraint |
| --- | --- | --- |
| `template` | Base template name | `ubuntu-24.04` (the contract's example value) |
| `class` | `persistent` (durable by default when object storage is enabled; a bundle is emitted on pause) or `ephemeral` (disposable) | persistent if omitted |
| `vcpu` / `mem` | Size; `mem` is in MiB (the `mem` in info) | Defaults are decided by the node, see CONTRACT §1 |
| `disk` | Writable layer size (MiB) | Counted as 512 at quota admission; the actual value is whatever the response says |
| `ttl_idle_s` / `ttl_max_s` | How long idle before automatic pause / upper limit on total lifetime (seconds) | See CONTRACT §1 |
| `qos` | Node scheduling class | `normal` when `priority=interactive`, otherwise `be` |
| `priority` | `interactive` / `batch` / `background`; batch / background are placed only on nodes that still have 2 free slots and more than 1 GiB of free memory, and never preempt others | `interactive`; any other value → 400 |
| `boot` | `auto` / `cold` / `snapshot` | `auto` |
| `layers` | Ids of read-only layers stacked on top of the base image | None |
| `network` | Egress policy (shape in [network.md](network.md)); if absent, the Project default is used ([tenancy.md](tenancy.md)) | None → node default |
| `retain_paused_s` | How long to keep the sandbox after it pauses (including idle auto-pause) before deleting it; `0` = keep forever. A durable sandbox has already emitted a bundle at pause, so it can be imported again after deletion | `0` |
| `env` | Base environment variables for every exec, ≤ 64 pairs; the request's own `env` is layered on top | None |
| `volumes` | Volume ids or `{id, mount?, ro?}`; mount rules are in [state.md](state.md) | None |
| `metadata` | Key-values, recorded on the route; the node does not see them | None |
| `node` | Pin the placement node (whether non-admins may use it: see CONTRACT §1); pinned placement is not affected by cordon | The control plane picks by scoring |
| `template_key` | A template name or a 16-hex-digit placement hint. When a template name is given, any unspecified `template` / `vcpu` / `mem` / `layers` / `disk` etc. are taken from the template (D75); specified ones must match the template, otherwise 400 (a template is a snapshot of one exact size); to change the size, add `"boot": "cold"`: cold-start using the template's base and layers with whatever size you want, but state in the template's memory (a running database, a warmed cache) does not come along (D80) | None |

Placement: the warm pool / template snapshot comes first, then the ratio of free slots to free memory. When an interactive sandbox finds no room, the control plane first pauses one background (then batch) sandbox, and failing that terminates one ephemeral background / batch sandbox (audit `preempt` / `preempt_destroy`); persistent ones are only paused, never killed.

Response **201**: the sandbox info (including `node`, `project`, `priority`, `metadata`).

| Error | Meaning |
| --- | --- |
| 400 | Invalid network policy; wrong priority; `template_key` inconsistent with the template configuration |
| 402 | `quota: daily budget` |
| 403 / 404 | Project is not yours / does not exist; volume not of this project is 403; volumes on different nodes is 404 |
| 409 | `node` inconsistent with the node the volume is on |
| 429 | `quota: sandboxes` / `quota: storage` / `no node with capacity`; the volume already has a writer |
| 502 / 503 | Node unreachable; node draining |

```bash
curl -s -X POST $CP/sandboxes -H "Authorization: Bearer $TOKEN" -H "X-Project: web" \
  -d '{"template":"ubuntu-24.04","vcpu":2,"mem":1024,"ttl_idle_s":600,"env":{"MODE":"dev"}}'
```


**`start_cmd`** (D71, optional): a command started **once** in the background in the sandbox after creation succeeds, for example `"python3 -m http.server 8080 --bind 0.0.0.0"`. It runs as the agent user via `nohup sh -c '…'`, with output going to `/home/agent/start.log`; the response gains a `start`: `{"started": true, "pid": "123", "log": "/home/agent/start.log"}`, or `{"started": false, "error": …}` -- **failure to start does not make creation fail** (the sandbox is already there; check the log and handle it yourself). It runs only once, at creation: pause / resume keeps the process through memory retention and does not rerun it; process state after migration / import from a bundle / checkpoint rollback follows memory and is not rerun. To wait for the service to become ready, poll `ports/<port>/` yourself. Together with [`ports`](#ports) and [preview domains](../production.md), this is "one command to open an accessible web service".
## GET /sandboxes

Caller. Members list only their own account's; admin lists all. Response `{"sandboxes": [info…], "total": N}`.

**Filtering and pagination** (D70, all optional; without them it is the original "everything"): `?status=running` (repeatable), `?metadata=job:42` (repeatable, matches only if **all** are satisfied), `?q=text` (substring search in id / template / node / metadata, case-insensitive), `?limit=` and `?offset=` (when either is used, ordered by creation time, newest first). `total` is the count after filtering and before pagination. Example: `GET /sandboxes?status=running&metadata=team:red&limit=20`.

## GET /sandboxes/\<id\>

Caller. Response is the info, plus `disk_used_mib` (actually allocated), `egress:{out, in}` (cumulative bytes through the egress proxy), `checkpoints` (list of names), `volumes`, `template_snapshot`. 404: does not exist or is not yours.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/sandboxes/sb-123
```

## DELETE /sandboxes/\<id\>

Caller. Response `{"deleted": "<id>"}`; the control plane then deletes the route. Attached volumes are detached automatically.

## POST /sandboxes/\<id\>/pause · resume

Caller. Response: info. While paused only disk is billed; after `resume`, `status=running`. A paused sandbox that is accessed by a port request is transparently resumed.

## POST /sandboxes/\<id\>/ttl

Request: `{"ttl_idle_s?": 600, "ttl_max_s?": 7200, "retain_paused_s?": 3600}`. Response: info.

```bash
curl -s -X POST $CP/sandboxes/sb-123/ttl -H "Authorization: Bearer $TOKEN" -d '{"ttl_idle_s":900}'
```

## POST /sandboxes/\<id\>/exec

Caller. Executes one command synchronously.

Request:
```json
{"cmd": "python3 -V", "user": "agent", "cwd": "/work", "timeout": 60, "env": {"A": "1"}, "stdin": "…", "landlock": true}
```
- `cmd` is required; `user`: `agent` (the default low-privilege user) or `root`; `timeout` is in seconds; `env` is layered on top of the `env` from creation.
- `landlock`: `true` = the default harness constraints (whole disk read-only, with cwd / tmp / home writable, and TCP allowed only to the egress proxy at 3128); or `{"ro": [], "rw": [], "connect_ports": []}` for custom.

Response:
```json
{"exit": 0, "stdout": "Python 3.12.3\n", "stderr": "", "timeout": false, "elapsed_s": 0.12}
```
- If the process was SIGKILLed (`exit` is 137 when going through a shell; a child process killed directly by a signal is -1) and the kernel log has a just-occurred `Out of memory: Killed process`, the response gains two more fields: `"oom_killed": true`, `"hint": "govulncheck was killed by the kernel: out of memory in a 2048 MiB sandbox; create it with more mem"` (plus `oom: {pid, process}`). Only the synchronous `exec` has this hint. (D79)
Errors: 404; 503 node down.

```bash
curl -s -X POST $CP/sandboxes/sb-123/exec -H "Authorization: Bearer $TOKEN" -d '{"cmd":"uname -a","user":"root"}'
```

## POST /sandboxes/\<id\>/exec/stream

The request is the same as `exec`. The response is chunked, with one line of JSON per chunk:
```
{"event":"out","data":"…"}
{"event":"err","data":"…"}
{"done":true,"exit":0,"timeout":false,"elapsed_s":3.2}
```
The event names on the wire are `"out"` / `"err"` (the Python SDK reads them as stdout / stderr); the last line is `done`.

```bash
curl -sN -X POST $CP/sandboxes/sb-123/exec/stream -H "Authorization: Bearer $TOKEN" -d '{"cmd":"for i in 1 2 3; do echo $i; sleep 1; done"}'
```

## Sessions (pty)

| Endpoint | Request | Response |
| --- | --- | --- |
| `POST /sandboxes/<id>/sessions` | `{"user?": "agent", "pty?": true}` | **201** `{session, pty}` |
| `POST /sandboxes/<id>/sessions/<s>/exec` | `{"cmd": "…", "timeout?": 30}` | `{exit, stdout, …}` |
| `POST /sandboxes/<id>/sessions/<s>/write` | `{"data_b64": "…"}` | See CONTRACT §1 |
| `POST /sandboxes/<id>/sessions/<s>/read` | `{"wait?": 1}` (seconds) | `{"data_b64": "…", "bytes": 12}` |
| `DELETE /sandboxes/<id>/sessions/<s>` | — | See CONTRACT §1 |

A session keeps shell state (cd, environment variables). With `pty:true`, `write`/`read` send and receive terminal bytes (base64).

```bash
S=$(curl -s -X POST $CP/sandboxes/sb-123/sessions -H "Authorization: Bearer $TOKEN" -d '{"pty":true}' | jq -r .session)
curl -s -X POST $CP/sandboxes/sb-123/sessions/$S/write -H "Authorization: Bearer $TOKEN" -d '{"data_b64":"'$(printf 'ls\n' | base64)'"}'
curl -s -X POST $CP/sandboxes/sb-123/sessions/$S/read  -H "Authorization: Bearer $TOKEN" -d '{"wait":1}'
```

## files

| Endpoint | Description |
| --- | --- |
| `PUT /sandboxes/<id>/files?path=/abs/path` | body = raw bytes; response `{"bytes": n}` |
| `GET /sandboxes/<id>/files?path=/abs/path` | response = raw bytes (`application/octet-stream`) |

Reads and writes are of the **whole file**: the node reads from the guest in 4 MiB chunks until a short read; the cap is **1 GiB**. `path` must be an absolute path. The request body is also limited by `CP_MAX_BODY_MIB` (default 512); exceeding it → 413.

```bash
curl -s -X PUT "$CP/sandboxes/sb-123/files?path=/work/a.txt" -H "Authorization: Bearer $TOKEN" --data-binary @a.txt
curl -s "$CP/sandboxes/sb-123/files?path=/work/a.txt" -H "Authorization: Bearer $TOKEN" -o a.txt
```

## POST /sandboxes/\<id\>/fork

Caller. Clones n copies from the parent sandbox (the parent is untouched).

Request: `{"n?": 1, "class?": "ephemeral", "ttl_idle_s?", "ttl_max_s?", "qos?", "checkpoint?": "name"}`. With `checkpoint`, the clones start from that checkpoint's memory and writable layer.

Response **201** `{"clones": [info…], "checkpoint": "…"}`; the clones automatically become routes, belonging to the parent sandbox's Project. Errors: 404 unknown checkpoint.

```bash
curl -s -X POST $CP/sandboxes/sb-123/fork -H "Authorization: Bearer $TOKEN" -d '{"n":3,"checkpoint":"clean"}'
```

## Checkpoints and rollback

| Endpoint | Request | Response |
| --- | --- | --- |
| `GET /sandboxes/<id>/checkpoints` | — | `{"checkpoints": [{name, note, created, paused_s, chain_len, upper_mib}]}` |
| `POST /sandboxes/<id>/checkpoints` | `{"name?": "clean", "note?": "…"}` | **201** entry |
| `POST /sandboxes/<id>/rollback` | `{"checkpoint": "clean"}` | 200 info + `{rollback_s, checkpoint}` |
| `DELETE /sandboxes/<id>/checkpoints/<name>` | — | See CONTRACT §1 |

- Taken while **running**: saves device state, one Diff memory layer, and a sparse copy of the writable layer, then keeps running, with a pause of a few hundred milliseconds. Names match `[a-z0-9][a-z0-9_.-]{0,39}`; a duplicate name is 409.
- Rollback is **in place**: it kills processes, swaps the writable layer back, and restores from the checkpoint's memory chain; the id, directory, tap, and IP do not change.
- Memory layers referenced by a checkpoint are pinned and are not reclaimed by compaction or rollback. Bundles (export / migrate / evacuate) carry the checkpoints, and rollback still works after import.

```bash
curl -s -X POST $CP/sandboxes/sb-123/checkpoints -H "Authorization: Bearer $TOKEN" -d '{"name":"clean","note":"after setup"}'
curl -s -X POST $CP/sandboxes/sb-123/rollback    -H "Authorization: Bearer $TOKEN" -d '{"checkpoint":"clean"}'
```

## PUT /sandboxes/\<id\>/network

Caller: a member's tightening takes effect immediately, loosening goes through approval; owner / admin change it directly.

Request: `{"mode?": "closed|allowlist|audited", "allow_hosts?": […], "allow_direct?": […], "rules?": […]}`; `limits?` may also be included (rule syntax in [network.md](network.md)). The node merges by key, and keys not sent are untouched. Response `{"network": {…}}`.

**Approval flow**: if a member's request "loosens" the current policy (meaning in [network.md](network.md)), the control plane does not push it to the node but records a pending request and returns **202**:
```json
{"request": {"id": "nr-…", "sandbox": "sb-123", "project": "web", "by": "bob", "network": {…}, "status": "pending"}, "note": "loosening the policy needs an owner: POST /network/requests/<id>/approve"}
```
An owner / admin then approves or denies it at `/network/requests`. Tightening returns 200 directly.

Errors: 400 invalid policy / rule (validated by both the control plane and the node); 404.

```bash
curl -s -X PUT $CP/sandboxes/sb-123/network -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"allowlist","allow_hosts":["api.github.com"]}'
```

## GET /sandboxes/\<id\>/metrics

Live metrics (D57). Every 5 seconds the node samples the sandbox cgroup's CPU and memory (plus cumulative outbound bytes) into an in-memory ring buffer (one hour by default, 720 points); `?since=<unix seconds>` returns only newer points. Response:
```json
{"sandbox": "sb-…", "interval_s": 5.0, "points": [{"t": 1767225600.0, "cpu_pct": 12.5, "mem_mib": 183.2, "egress_out": 10240, "egress_in": 20480}]}
```
`cpu_pct` is a percentage of **one core** (a fully loaded 2-vCPU sandbox is 200); the first point has none (no previous sample to compare with, so it is null). Outbound is a cumulative value; take differences yourself for a rate. While the sandbox is paused there are no points (a gap in the curve); the buffer is cleared when the node restarts; a dev sandbox not running inside a jailer has no cgroup and yields no points. The console's sandbox detail "Metrics" page uses this to draw three lines.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/sandboxes/$ID/metrics?since=$(( $(date +%s) - 300 ))"
```

## GET /sandboxes/\<id\>/egress

Caller. The most recent 200 egress decisions: `{"decisions": [{sandbox, method, scheme, host, port, path, allowed, rule, injected}]}`.

## ports

`ANY /sandboxes/<id>/ports/<port>/<path>?<query>`: reverse proxy to the guest's `ip:port` (plaintext HTTP, the whole response).

- A paused sandbox is transparently resumed first.
- A request with `Upgrade: websocket` becomes a **bidirectional byte tunnel** after the handshake (the control plane hijacks the connection and dials the node, and the node dials the guest; if the guest does not reply 101, its answer is passed through unchanged). Audit `port_tunnel` is written.
- Errors: 502 nothing listening on the port / node unreachable; 400 bad port.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/sandboxes/sb-123/ports/8000/health
```

## Budget, ticket, inbox, events, diff

These are the host side of the agent-runtime kernel ABI (contract §7).

| Endpoint | Request | Response |
| --- | --- | --- |
| `PUT /sandboxes/<id>/budget` | `{"max_tokens?": n, "max_usd?": x}` | `{"budget": {…}}` |
| `PUT /sandboxes/<id>/ticket` | `{"ticket": {id, short, text, scope, branch, base_branch, base_commit, cwd, rules, inputs, thread}}` | `{"ticket": {…}}`; written to the guest's `/run/aios/ticket.json` |
| `POST /sandboxes/<id>/inbox` | `{"kind": "…", "text": "…", "actor?": "…", "thread?": "…"}` | **201** `{"n": 3}`; appended to `/run/aios/inbox.jsonl` |
| `GET /sandboxes/<id>/events?since=<seq>&max=` | — | `{"events": […], "last_seq": n}` |
| `GET /sandboxes/<id>/diff?cwd=/work/repo&base=main` | — | `{exit, stat, diff, stderr, base, cwd}`; runs `git diff <base>...HEAD` as root inside the sandbox |

```bash
curl -s -X PUT $CP/sandboxes/sb-123/budget -H "Authorization: Bearer $TOKEN" -d '{"max_usd":2}'
curl -s "$CP/sandboxes/sb-123/events?since=0" -H "Authorization: Bearer $TOKEN"
```

## POST /sandboxes/\<id\>/pack

Packs the sandbox's writable layer into a read-only layer. Request `{"name": "my-layer"}`; response **201** layer metadata `{id, name, bytes, …}`. It can then be used as `layers:[id]` when creating new sandboxes, see [images.md](images.md).

## Committing as a template: POST /builds {kind:"commit"}

Commits the sandbox's state at this moment (memory + writable layer) as a named template, through the build interface, see [images.md](images.md). On the node side there is also `POST /sandboxes/<id>/commit` (internal).

## POST /sandboxes/\<id\>/migrate

**Admin only**. Request `{"to?": "node-b"}` (if omitted, the control plane chooses); for the response see CONTRACT §1, which includes `volumes:[id]` when volumes are attached. Attached volumes are first exported to the target node; if a volume is still attached to another sandbox, it returns **409** and nothing is touched; on success the source node's copy is deleted and the route is switched to the new node. Checkpoints are carried. Errors: 403 non-admin.

```bash
curl -s -X POST $CP/sandboxes/sb-123/migrate -H "Authorization: Bearer $TOKEN" -d '{"to":"node-b"}'
```

## POST /sandboxes/\<id\>/export

Caller. Exports the bundle of a durable sandbox (with checkpoints). For response fields see CONTRACT §1; to find existing bundles use `GET /store/bundles` ([state.md](state.md)).

## POST /sandboxes/import

**Admin only**. Re-lands a durable sandbox from a bundle onto a node, in state `paused`, with the route recorded under the specified Project.

Request: `{"bundle": "<bundle>", "node?": "node-b", "project?": "web"}`. Response **201** info. Errors: 403; 404 Project.

```bash
curl -s -X POST $CP/sandboxes/import -H "Authorization: Bearer $TOKEN" -d '{"bundle":"<bundle>","node":"node-b","project":"web"}'
```
