# Cairn

**A place where an agent can try things safely: when it goes wrong, go back to the last marker; when it reaches too far, a person says yes; what it did is on record.**

Agents write code, run commands and install packages. They get things wrong, and one day they will do something they should not. Cairn puts them in a microVM you can rewind:

* **Rewind.** Take a checkpoint of a running sandbox (tens of milliseconds), roll it back in place (same id, same address), or fork n copies from any checkpoint to try different things.
* **No way out unless approved.** A sandbox has no direct network. Outbound requests go through a proxy that allows only listed hosts; a member who wants to loosen the list needs an owner's approval; credentials can be injected at the proxy so they never enter the sandbox.
* **On record.** Everything the control plane does, including each `exec` and file operation, goes into a hash chain you can verify.
* **Knows who it works for.** Accounts, projects, members, quotas, prepaid credit and usage billing live in the sandbox layer.

Underneath are Firecracker microVMs under the jailer. On top are a control plane, Python and JS SDKs, a CLI, an MCP server and a web console. There is also an optional **agent layer**:
a hosted personal coding agent that the sandbox knows by its budget, tickets and events. It talks to the sandbox cloud only through the HTTP API / SDK; the sandbox cloud runs without it.

## Five minutes

```bash
scripts/bootstrap.sh && cd deploy && docker compose up -d     # Firecracker, images, kernel, base image, secrets; then the whole stack
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

Needs Linux x86_64 with `/dev/kvm`, Docker 24+ with compose v2, about 15 GB of disk; the first install compiles a guest kernel (30-60 minutes).
For agents: `claude mcp add cairn -- python3 -m cairnbox.mcp`. An E2B-shaped REST surface listens on port 18710, so common E2B client code works by changing the address.

## Honest status

Measured on one host. **No external penetration test**, no published baseline against other sandboxes, multi-node not load-tested. `scripts/bootstrap.sh` has not been run end to end from a clean machine
(the steps were verified separately; see the end of [docs/en/quickstart.md](docs/en/quickstart.md)), and nobody outside the project has followed the instructions yet. What is missing: [ROADMAP.md](ROADMAP.md).

Docs: [docs/en/](docs/en/README.md) (quickstart, architecture, security model) · [user guide](docs/guide/README.md) and [design decisions](docs/decisions-index.md) (Chinese).
License: Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
