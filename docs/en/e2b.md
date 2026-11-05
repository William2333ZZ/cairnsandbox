# Using the official E2B SDKs with Cairn

Point the E2B SDKs at the control plane's E2B port (18710 by default); nothing else changes in your code.

```bash
export E2B_API_URL=http://<host>:18710
export E2B_SANDBOX_URL=http://<host>:18710     # the SDKs send every sandbox request here, told apart by an E2b-Sandbox-Id header
export E2B_API_KEY=<a member token or the control plane token>
```

```python
from e2b import Sandbox
sbx = Sandbox.create(timeout=300)
print(sbx.commands.run("echo hi").stdout)
```

## What was verified

The official `e2b` Python SDK 2.63.0 (33 cases) and the npm `e2b` package 2.63.0 (20 cases) pass a conformance suite against this port: `ops/e2b-conformance.py` and `ops/e2b-conformance.mjs`, results in [`docs/research/e2b-conformance/`](../research/e2b-conformance). It ran on **one Linux host with two node containers** (Firecracker, jailer on). Covered: `commands.run` with output streamed as it is produced, exit codes, `envs`/`cwd`/`user`, background commands (`list`, `kill`, `connect`), stdin, PTY (create, input, resize, kill), files (text, bytes, 20 MiB, `write_files`, list, exists, info, make_dir, rename, remove, `watch_dir`), pause and `connect`, `set_timeout`, `get_metrics`, fork, snapshots (create, start from one, list, delete), `update_network`, list filters.

Also checked: sandboxes spread over two nodes, and a paused sandbox moved by `evacuate` after its node was killed (`ops/e2b-multinode-check.py`); a command sent to a paused sandbox resumes it. On the control plane's own host, `bash -l -c true` takes about 9 ms (p50) from `Start` to its end event, 1 KiB file reads and writes about 4 ms, and four sandboxes ran 100 commands in parallel without errors (`ops/e2b-latency-raw.py`; single host, small scale, not a benchmark against anything).

Not verified: other SDK versions or languages (Go, Java, ...), two physical machines, load beyond four parallel sandboxes. A new SDK release can change request shapes, so rerun the suite after upgrading.

## What differs from E2B

- No template builds (`Template.build()`), no `gzip` uploads, no `StreamInput` (client streaming needs HTTP/2; `send_stdin` is unaffected).
- `timeout` is an *idle* timeout, not an absolute end time. The default command user is `agent`, not root (`user="root"` works). Without `allow_internet_access` the project's default egress policy applies; E2B opens the internet by default.
- Changing a sandbox's network through this port needs an owner or admin: a member loosening egress needs approval, which only the native API queues.
- `get_host(port)` works when the control plane has `PREVIEW_DOMAIN` set (it is reported as the sandbox's `domain`, and a wildcard DNS name plus TLS in front are yours to provide); unlike E2B it is not public, it needs a token. `watch_dir` polls (every 250 ms), it has no inotify.
- Sandboxes need a base image built with the envd-capable `sandboxd` (rebuild `recipes/base-debian-13`); templates created before that need rebuilding.

## What E2B does not have

A background process survives pause, checkpoint, rollback and fork, because it lives in the VM's memory: after a rollback, `sbx.commands.list()` still shows it and `sbx.commands.connect(pid)` re-attaches to the same process.

## How it works

The SDK's data-plane requests arrive on the same port, carry `E2b-Sandbox-Id` and an access token bound to that one sandbox, and are relayed through the node and over vsock to `sandboxd`, which speaks the envd protocol (Connect with the JSON codec, plus `/files`) on a vsock-only listener. The control plane audits each request (for `Start`, the command line). The full matrix is in [`docs/guide/api/e2b.md`](../guide/api/e2b.md) (Chinese), the decision in D88.
