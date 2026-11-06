# CairnSandbox documentation (English)

CairnSandbox has two layers: a **sandbox cloud**, which is useful on its own, and an optional **agent layer** that runs on top of it through the public API only. These pages are about the sandbox cloud unless they say otherwise; the agent layer is described in [the README](../../README.md#how-it-fits-together).

The numbered design documents (`docs/000`-`013`) and the design decisions are written in Chinese. The user guide is available in both languages: this directory holds the English version ([`guide/`](guide/README.md)), [`../guide/`](../guide/README.md) the Chinese one, with the same layout.

| Page | For |
| --- | --- |
| [quickstart.md](quickstart.md) | installing on a Linux host and running your first sandbox |
| [architecture.md](architecture.md) | what the parts are and how a request flows through them |
| [e2b.md](e2b.md) | using the official E2B SDKs against CairnSandbox: what passes, what differs |
| [multi-machine.md](multi-machine.md) | adding node machines to a control plane by IP (verified on one host only) |
| [security-model.md](security-model.md) | what is isolated how, who is trusted, what is not done |
| [guide/](guide/README.md) | the user guide: tenant manual, API reference, Python and JS SDKs, MCP, going to production, operations, tutorials |

Also in English: [`../../README.md`](../../README.md), [`../../ROADMAP.md`](../../ROADMAP.md), [`../../SECURITY.md`](../../SECURITY.md),
[`../../CONTRIBUTING.md`](../../CONTRIBUTING.md). Machine-readable API and SDK references: `sdk/python/cairnsandbox/`, `sdk/js/src/`.
The design documents are not translated yet; pull requests that translate one are welcome.
