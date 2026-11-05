# Architecture

> CairnSandbox (Cairn for short) has two layers: a **sandbox cloud**, which is useful on its own, and an optional **agent layer** that runs on top of it through the public API only. This page describes the sandbox cloud (control plane, node agent, microVMs); the optional agent layer (`aios/`, `intake/`, `eval/`, `web/`) sits on top of its HTTP API.

```
 client / SDK / CLI / MCP / console ──HTTP (bearer)──▶ control plane (Go, cp/)
                                                            │ accounts, projects, quotas, metering, audit chain, scheduling, E2B surface
                                                            ▼ HTTP (node token)
                                                       node agent (Python, node/)  ── Firecracker + jailer ──▶ microVM
                                                            │                                                   │ vsock
                                          tap + nftables + egress proxy   chunk store (images, bundles)         ▼
                                                                                                         sandboxd (Rust, guest/)
```

| Part | Where | Role |
| --- | --- | --- |
| Control plane | `cp/` | One Go binary. Multi-tenant API (account > project > member, named API keys), quotas, usage metering and prepaid credit, scheduling across nodes, the hash-chained audit log, the E2B-compatible surface, webhooks, the web console host. Stateless: state is in Postgres, the registry and the nodes, so replicas can be added. |
| Node agent | `node/` | Runs on each host (as a privileged container). Boots and restores microVMs through Firecracker under the **jailer** (chroot, unprivileged uid, per-VM cgroup), manages checkpoints / rollback / fork, templates and layers, per-VM networking (tap + nftables, no direct internet), and the **egress proxy** that enforces each sandbox's host allowlist. |
| Guest | `guest/` `recipes/` | A trimmed Linux kernel (no modules, no io_uring, no user namespaces), an init that builds an overlay root (read-only base + read-only layers + a writable layer), and `sandboxd`, a static Rust daemon that serves exec, sessions, PTY, files and ports over vsock. |
| Chunk store | `node/imgstore.py` | Content-addressed chunks, local directory or any S3-compatible bucket (Garage in `deploy/`). Holds base images, layers, paused sandboxes and template snapshots; nodes load images lazily from it. |
| Registry / DB | `deploy/` | Valkey (routes, nodes, leases) and PostgreSQL (tenancy, usage, billing, builds). |
| Console | `console/` | Next.js static export served by the control plane. |
| SDKs, CLI, MCP | `sdk/` | Python (`cairnsandbox`) and JS/TS (`cairnsandbox`), zero runtime dependencies. |
| Agent layer (optional) | `aios/` `intake/` `eval/` `web/` | A coding agent, its intake service and evaluator. It uses the sandbox cloud only through the HTTP API / SDK; the sandbox cloud runs without it. |

## State and time travel

A sandbox's memory is a chain of sparse snapshot files; its disk is a writable layer on top of shared read-only layers.
A **checkpoint** pauses the VM briefly, writes a memory diff and copies the writable layer. **Rollback** restarts the VM process from that point in place
(same id, same address); **fork** starts clones from it, each with its own copy of the writable layer. A **template** is a committed checkpoint that new
sandboxes are born from (warm starts in tens of milliseconds, cold boot in a few hundred).
Restoring a chain of more than one memory file currently merges it into one file (once per checkpoint); lazy restore through userfaultfd exists in the code but needs a host setting (`docs/decisions.md` D81).

## Egress

A sandbox has no route to the internet. Outbound HTTP(S) goes through the node's proxy, which allows only hosts on the sandbox's allowlist and records each decision.
Modes are `closed`, `allowlist` and `audited`. Tightening is immediate; loosening by a project member becomes a pending request that an owner or admin approves.

## Where to read more

Design decisions with their reasoning: `docs/decisions-index.md` (Chinese). Deployment: `deploy/README.md`. Security: [security-model.md](security-model.md).
