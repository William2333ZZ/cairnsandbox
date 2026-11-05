# CairnSandbox documentation (English)

CairnSandbox has two layers: a **sandbox cloud**, which is useful on its own, and an optional **agent layer** that runs on top of it through the public API only. These pages are about the sandbox cloud unless they say otherwise; the agent layer is described in [the README](../../README.md#how-it-fits-together).

Most of the design documents are written in Chinese (`docs/000`-`013`, `docs/guide/`). This directory holds the English entry points.
The API is plain HTTP and the SDKs are self-describing, so most people never need the Chinese text.

| Page | For |
| --- | --- |
| [quickstart.md](quickstart.md) | installing on a Linux host and running your first sandbox |
| [architecture.md](architecture.md) | what the parts are and how a request flows through them |
| [e2b.md](e2b.md) | using the official E2B SDKs against CairnSandbox: what passes, what differs |
| [multi-machine.md](multi-machine.md) | adding node machines to a control plane by IP (verified on one host only) |
| [security-model.md](security-model.md) | what is isolated how, who is trusted, what is not done |

Also in English: [`../../README.md`](../../README.md), [`../../ROADMAP.md`](../../ROADMAP.md), [`../../SECURITY.md`](../../SECURITY.md),
[`../../CONTRIBUTING.md`](../../CONTRIBUTING.md). Machine-readable API and SDK references: `sdk/python/cairnsandbox/`, `sdk/js/src/`.
Translation of the rest is on the roadmap; pull requests that translate a page are welcome.
