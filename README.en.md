# Cairn Sandbox Cloud

Firecracker microVM sandboxes for AI agents: sub-second warm starts, **checkpoints, in-place rollback and fork**, volumes, L7 egress policy with owner approval, multi-tenant control plane, metering and billing, a web console, Python and JS/TS SDKs, a CLI and an MCP server. (The repository also contains the Cairn agent layer; this page is about the sandbox cloud. Most documentation is in Chinese; the API is plain HTTP and the SDKs are self-describing.)

## Five minutes

```bash
scripts/bootstrap.sh && cd deploy && docker compose up -d     # Firecracker, images, kernel, base image, secrets; then the whole stack
pip install -e sdk/python                                                         # or: cd sdk/js && npm install && npm run build
```

```python
from cairnbox import Client
c = Client("http://127.0.0.1:18700", token="…", project="acme")
with c.sandboxes.create(mem=512, network={"allow_hosts": ["pypi.org"]}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
    sb.checkpoints.create("clean"); sb.commands.run("rm -rf /home/agent/*"); sb.checkpoints.rollback("clean")
    clone = sb.fork(checkpoint="clean")[0]
```

```ts
import { Client } from "cairnbox";
const c = new Client({ url: "http://127.0.0.1:18700", token: "…" });
const sb = await c.sandboxes.create({ mem: 512 });
console.log((await sb.commands.run("uname -a")).stdout);
await sb.kill();
```

```bash
cairn create --mem 512        # CLI (installed with the Python SDK)
cairn exec sb-1a2b -- python3 -V
```

For agents: `claude mcp add cairn -- python3 -m cairnbox.mcp`. E2B-shaped REST on a second port, so most E2B client code works by pointing it at port 18710.

## What it does

- **Time travel**: live checkpoints (≈50 ms), in-place rollback, fork from a checkpoint, migration that carries state.
- **Isolation**: Firecracker + jailer, trimmed guest kernel, seccomp / Landlock in the guest, no DNS and no direct network: every outbound request goes through an L7 egress proxy (closed / allowlist / audited), with rules, per-sandbox limits, credential injection, and owner approval for any relaxation.
- **Tenancy**: account ⊃ project ⊃ member, quotas, named API keys (single-project, read-only option), invite-based signup, prepaid credit, account suspension, abuse signals, hash-chained audit log.
- **Operations**: multi-replica control plane (Redis + Postgres), S3-compatible chunk store (Garage), live per-sandbox metrics, build logs, public `/status`, optional node TLS, node self-fencing with optional auto-evacuation, Prometheus metrics and alert rules.

## Honest status

Usable for your own and trusted teams. **Not yet** for untrusted public tenants or paid service: no external penetration test, node failover and TLS not drilled on real multi-machine setups, no payment-provider integration, single storage node. The exact list, with owners, is in [docs/research/commercialization-gap-2026-11.md](docs/research/commercialization-gap-2026-11.md) and the security checklist at the end of [docs/013-threat-model.md](docs/013-threat-model.md). Pricing context: [docs/research/pricing-2026-11.md](docs/research/pricing-2026-11.md).

Docs: [user guide](docs/guide/README.md) · [API reference](docs/guide/api/README.md) · [JS SDK](docs/guide/sdk-js.md) · [decisions](docs/decisions-index.md) · [changelog](CHANGELOG.md)

License: Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md). Roadmap: [ROADMAP.md](ROADMAP.md). English docs: [docs/en/](docs/en/README.md).
