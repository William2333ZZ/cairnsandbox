# Quickstart

## What you need

* A Linux x86_64 host with **`/dev/kvm`** (bare metal, or a cloud VM with nested virtualization). Docker 24+ with compose v2. About 15 GB of disk.
* 30-60 minutes the first time: the guest kernel is compiled from source. (Prebuilt kernel and base image as release artifacts are on the roadmap.)
* ARM64 is not supported yet.

## Install

```bash
git clone https://github.com/William2333ZZ/cairn && cd cairn
scripts/bootstrap.sh          # Firecracker, docker images, guest kernel, guest runtime, base image, secrets, console, chunk store
cd deploy && docker compose up -d
curl -s -H "Authorization: Bearer $(cat secrets/cp.token)" http://127.0.0.1:18700/health
```

Everything listens on 127.0.0.1. Put a reverse proxy with TLS in front of it to expose it (`deploy/README.md`).
Behind a regional mirror, export `APT_MIRROR`, `PIP_INDEX`, `GOPROXY`, `CARGO_MIRROR`, `KERNEL_URL` before `bootstrap.sh` (see the header of that script).

## First sandbox

```bash
pip install -e sdk/python
export CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=$(cat deploy/secrets/cp.token)
python3 sdk/python/examples/quickstart.py
```

```python
from cairnbox import Client
c = Client("http://127.0.0.1:18700", token="...", project="acme")
with c.sandboxes.create(mem=512, network={"allow_hosts": ["pypi.org"]}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
    sb.checkpoints.create("clean")
    sb.commands.run("rm -rf /home/agent/*")
    sb.checkpoints.rollback("clean")          # the files are back
    clone = sb.fork(checkpoint="clean")[0]    # an independent copy from the same point
```

The web console is at `http://127.0.0.1:18700/ui/` (log in with the same token). The CLI is `cairn` (installed with the Python SDK);
for agents, `claude mcp add cairn -- python3 -m cairnbox.mcp`. An E2B-shaped REST surface listens on port 18710.

## When something fails

* `/dev/kvm` missing or not readable by Docker: the node container will not start. Check `docker logs fc-node`.
* The first sandbox is slow: templates are built at node start; give it a minute and look at `GET /health`.
* Egress denied: the sandbox's network starts **closed**. Ask for hosts with `PUT /sandboxes/<id>/network`; a project member's request to loosen it waits for an owner's approval.

## What is verified

A clean copy of this tree was built and run on one Linux host (Ubuntu 22.04, kernel 5.15, behind regional mirrors): the node and control-plane images (the control plane's `go vet` and tests run inside
its image build), the Python unit tests, the Rust guest runtime and the Debian base image built from source, then a compose stack (Valkey, Postgres, chunk store, node, control plane) and
`sdk/python/examples/quickstart.py`, which passed 13 of 13 steps (create, exec, files, streaming, checkpoint, rollback, fork, pause/resume, sessions, policy, usage).

**Not exercised from scratch:** `scripts/bootstrap.sh` itself, the Firecracker download (that host cannot reach GitHub, so the binaries were copied in), the guest kernel compile (a kernel built earlier was used),
and the console build. Nobody outside the project has followed these instructions yet; if a step fails for you, that is a bug in them. Please open an issue with the step and the error.
