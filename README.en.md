<div align="center">

<img src="docs/assets/banner.svg" alt="Cairn: a sandbox where agents can go back" width="100%">

<br>

[![ci](https://github.com/William2333ZZ/cairn/actions/workflows/ci.yml/badge.svg)](https://github.com/William2333ZZ/cairn/actions/workflows/ci.yml) [![license](https://img.shields.io/badge/license-Apache--2.0-14B8A6)](LICENSE) [![status](https://img.shields.io/badge/status-early_(0.x)-F59E0B)](#status) [![platform](https://img.shields.io/badge/runs_on-Linux_x86__64_+_KVM-64748B)](docs/en/quickstart.md) [![stack](https://img.shields.io/badge/built_with-Go_·_Python_·_Rust_·_Firecracker-64748B)](docs/en/architecture.md)

**[中文](README.md) · [English](README.en.md) · [Quickstart](docs/en/quickstart.md) · [Features](docs/guide/features.md) · [Roadmap](ROADMAP.md) · [Design decisions](docs/decisions-index.md)**

</div>

Agents write code, run commands and install packages. They get things wrong, and one day they will do something they should not. Cairn puts them in a microVM you can **rewind**.

| | |
| :-- | :-- |
| ⏪ **Rewind** | Checkpoint a running sandbox (a pause of tens of milliseconds), roll it back in place (same id, same address), or fork n copies from any checkpoint to try different things. |
| 🔒 **No way out unless approved** | A sandbox has no direct network. Outbound requests go through a proxy that allows only listed hosts; a member who wants to loosen the list needs an owner's approval; credentials are injected at the proxy and never enter the sandbox. |
| 🧾 **On record** | Everything the control plane does, including each `exec` and file operation, goes into a hash chain you can verify. |
| 👥 **Knows who it works for** | Accounts, projects, members, quotas, prepaid credit and usage billing live in the sandbox layer. |

<div align="center">
<img src="docs/assets/timeline.svg" alt="Checkpoint, rollback and fork" width="92%">
</div>

## Five minutes

```bash
scripts/bootstrap.sh && cd deploy && docker compose up -d
pip install -e sdk/python
CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=$(cat deploy/secrets/cp.token) python3 sdk/python/examples/quickstart.py
```

```python
from cairnbox import Client

c = Client("http://127.0.0.1:18700", token="...", project="acme")
with c.sandboxes.create(mem=512, network={"allow_hosts": ["pypi.org"]}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
    sb.checkpoints.create("clean")
    sb.commands.run("rm -rf /home/agent/*")
    sb.checkpoints.rollback("clean")                  # the files are back
    clone = sb.fork(checkpoint="clean")[0]            # an independent copy from the same point
```

```ts
import { Client } from "cairnbox";

const c = new Client({ url: "http://127.0.0.1:18700", token: "..." });
const sb = await c.sandboxes.create({ mem: 512 });
console.log((await sb.commands.run("uname -a")).stdout);
await sb.kill();
```

> [!NOTE]
> Needs Linux x86_64 with `/dev/kvm`, Docker 24+ with compose v2 and about 15 GB of disk. The first install compiles a guest kernel (30-60 minutes).
> For agents: `claude mcp add cairn -- python3 -m cairnbox.mcp`. An E2B-shaped REST surface listens on port 18710, so common E2B client code works by changing the address.

## How it fits together

<div align="center">
<img src="docs/assets/architecture.svg" alt="Architecture" width="92%">
</div>

Underneath are Firecracker microVMs under the jailer; on top are a control plane, Python and JS SDKs, a CLI, an MCP server and a web console. The **agent layer is optional**: a hosted personal coding agent
that the sandbox knows by its budget, tickets and events. It reaches the sandbox cloud only through the HTTP API or the SDK, and the sandbox cloud runs without it (`tests/unit/test_layers.py` enforces the boundary).

<div align="center">
<img src="docs/assets/shot-sandbox.png" alt="A sandbox" width="92%"><br><sub>One sandbox: size, network and limits, checkpoints with rollback and fork (console in Chinese)</sub>
<br><br>
<img src="docs/assets/shot-audit.png" alt="Audit" width="92%"><br><sub>Audit: everything the control plane did, verifiable as a hash chain</sub>
</div>

## Status

> [!WARNING]
> Measured on one host. **No external penetration test**, no published baseline against other sandboxes, multi-node not load-tested. `scripts/bootstrap.sh` has not been run end to end from a clean machine
> (the steps were verified separately; see the end of [docs/en/quickstart.md](docs/en/quickstart.md)), and nobody outside the project has followed the instructions yet. Do not treat it as the only barrier between untrusted public tenants and anything valuable.

What is missing: [ROADMAP.md](ROADMAP.md). Security problems: [SECURITY.md](SECURITY.md), not a public issue.

## Read on

| | |
| :-- | :-- |
| Using it | [docs/en/](docs/en/README.md): quickstart, architecture, security model · [user guide](docs/guide/README.md) (Chinese) · [features](docs/guide/features.md) |
| Reading the code | [docs/README.md](docs/README.md) · [docs/011](docs/011-architecture.md) index of parts · [design decisions](docs/decisions-index.md) (Chinese) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) (sign off your commits) · [Code of Conduct](CODE_OF_CONDUCT.md) |

<div align="center"><sub>Apache-2.0 · <a href="LICENSE">LICENSE</a> · third-party material in <a href="NOTICE">NOTICE</a></sub></div>
