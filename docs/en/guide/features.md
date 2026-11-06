# Feature List

The numbers come from single-machine measurements on a development machine and are not public baselines; for the full list of what is not done, see the [ROADMAP](../../../ROADMAP.md).

| | What is there | Numbers (dev machine, [bench](../../research/benchmark-2026-11.md)) |
| --- | --- | --- |
| Sandbox | Firecracker microVM + jailer; cold start / warm start from a template snapshot; automatic pause when idle, transparent resume; TTL and pause retention period; two classes, ephemeral / persistent, and three priority levels with preemption, interactive / batch / background | Cold start 396 ms, warm start **85 ms**, exec round trip 5 ms, no slowdown with 16 VMs |
| Time travel | Checkpoint a running sandbox, roll back in place (id / network unchanged), fork n sandboxes from a checkpoint, commit a running sandbox as a template | Checkpoint 47 ms, rollback 238 ms, fork 328 ms |
| Relocation | Migration (with checkpoints and volumes), takeover from a bundle after a node is lost, drain, rebalance, cordon | |
| File system | Read-only base + layers (recipe / OCI image / packed from a running sandbox) + writable layer; chunk store loaded on demand; independent volumes (single writer / multiple readers) | 60–72 MiB per idle VM (including guest pages) |
| Network | No DNS in the guest, everything goes through the egress proxy; closed / allowlist / audited; L7 rules (host wildcard / regex, path, method), credential injection (secrets never enter the sandbox), per-sandbox limits; loosening requires owner approval; port exposure (including WebSocket tunnels) | |
| Tenancy | Account ⊃ project ⊃ member; quotas (concurrency, storage, daily spend); project default network; token rotation and expiry; named API keys (single project, optionally read-only, revocable); self-service account opening with invitation codes; prepaid credit; account suspension and abuse detection (flag only); members see only their own account | |
| Metering | CPU seconds, memory MiB seconds, storage, egress bytes, model tokens; versioned price list; settlement ledger and CSV | |
| Operations | Console (Next.js, `/console/`) with live metric curves; public `/status`; node self-fencing and optional automatic takeover; preview domains; optional TLS between control plane and nodes; chunk store can land on S3-compatible storage (Garage); multi-replica control plane (Redis / Postgres shared state, nodes are the authority for runtime state); `/metrics`; audit hash chain; one-command compose deployment; upgrade as drain → swap image → uncordon | |
| Agent surface | Sandbox kernel ABI: budgets, tickets, inbox, event log, report; harness catalog; evaluation and evolution | |
