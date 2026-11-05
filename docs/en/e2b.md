# Using the official E2B SDKs with CairnSandbox

> This is the sandbox cloud acting as an E2B-compatible backend. You do not need the agent layer for any of it: the official SDKs talk to the control plane's E2B port, and you also get what E2B does not have (rewind, audit, approved-only egress).

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

The official `e2b` Python SDK (36 cases) and the npm `e2b` package (20 cases) pass a conformance suite, for each of six releases of both (2.52, 2.55, 2.57, 2.60, 2.62, 2.63; [matrix](../research/e2b-conformance/matrix-2026-11-05.json), run with `ops/e2b-matrix.sh`). The 1.x SDKs cannot be pointed at a custom API URL (they only reach e2b.app), so they are not supported and the suite refuses to send them anything. The suite against this port: `ops/e2b-conformance.py` and `ops/e2b-conformance.mjs`, results in [`docs/research/e2b-conformance/`](../research/e2b-conformance). It ran on **one Linux host with two node containers** (Firecracker, jailer on). Covered: `commands.run` with output streamed as it is produced, exit codes, `envs`/`cwd`/`user`, background commands (`list`, `kill`, `connect`), stdin, PTY (create, input, resize, kill), files (text, bytes, 20 MiB, `write_files`, list, exists, info, make_dir, rename, remove, `watch_dir`), pause and `connect`, `set_timeout`, `get_metrics`, fork, snapshots (create, start from one, list, delete), `update_network`, list filters.

Also checked: sandboxes spread over two nodes, and a paused sandbox moved by `evacuate` after its node was killed (`ops/e2b-multinode-check.py`); a command sent to a paused sandbox resumes it. On the control plane's own host, `bash -l -c true` takes about 9 ms (p50) from `Start` to its end event, 1 KiB file reads and writes about 4 ms, and four sandboxes ran 100 commands in parallel without errors (`ops/e2b-latency-raw.py`; single host, small scale, not a benchmark against anything).

Not verified: other SDK versions or languages (Go, Java, ...), two physical machines, load beyond four parallel sandboxes. A new SDK release can change request shapes, so rerun the suite after upgrading.

## What differs from E2B

- `Template.build()` works from CairnSandbox's own bases or from a template, with `run_cmd`, `copy`, env, workdir, user and a start/ready command; the control plane runs the steps in a build sandbox with the project's default egress policy (so `pip install` or `apt-get` fail unless the project allows them) and commits the running sandbox as the template. No tags, no images pulled from registries, uploads for COPY are kept on one replica. No sandbox logs endpoint; `gzip` uploads are decoded in memory (at most 32 MiB compressed, 64 MiB decompressed), everything else streams; no `StreamInput` (client streaming needs HTTP/2; `send_stdin` is unaffected).
- `timeout` is an *idle* timeout, not an absolute end time. The default command user is `agent`, not root (`user="root"` works). Without `allow_internet_access` the project's default egress policy applies; E2B opens the internet by default.
- Changing a sandbox's network through this port needs an owner or admin: a member loosening egress needs approval, which only the native API queues.
- `get_host(port)` works when the control plane has `PREVIEW_DOMAIN` set (it is reported as the sandbox's `domain`, and a wildcard DNS name plus TLS in front are yours to provide); unlike E2B it is not public, it needs a token. `watch_dir` polls (every 250 ms), it has no inotify.
- Sandboxes need a base image built with the envd-capable `sandboxd` (rebuild `recipes/base-debian-13`); templates created before that need rebuilding.

## code-interpreter

The official `e2b_code_interpreter` (`Sandbox.create()` and `run_code`, contexts) passes 17 cases for 2.10.5, 2.11.0 and 2.11.9 once the `code-interpreter-v1` template is built with `recipes/code-interpreter/build-template.py`. The server inside is our own small one (standard library only): stateful Python contexts, bash and JavaScript. With the optional `ci-pylibs` layer (numpy, pandas, matplotlib, seaborn, scipy, scikit-learn, pillow, requests, ...) matplotlib figures come back as PNG results; there is no structured `chart` object and no IPython magics. Details and limits: [`docs/guide/code-interpreter.md`](../guide/code-interpreter.md) (Chinese).

## What E2B does not have

A background process survives pause, checkpoint, rollback and fork, because it lives in the VM's memory: after a rollback, `sbx.commands.list()` still shows it and `sbx.commands.connect(pid)` re-attaches to the same process.

## How it works

The SDK's data-plane requests arrive on the same port, carry `E2b-Sandbox-Id` and an access token bound to that one sandbox, and are relayed through the node and over vsock to `sandboxd`, which speaks the envd protocol (Connect with the JSON codec, plus `/files`) on a vsock-only listener. The control plane audits each request (for `Start`, the command line). The full matrix is in [`docs/guide/api/e2b.md`](../guide/api/e2b.md) (Chinese), the decision in D88.
