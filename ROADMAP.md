# Roadmap

Honest list of what is missing, in rough priority order. Each item names where the reasoning lives. Nothing here is a promise of a date.

## Trust (blocks wider use)
- **Independent security review** of isolation, egress policy and tenancy. None yet. (`SECURITY.md`, `docs/013-threat-model.md`)
- **Baselines against other sandboxes** on the same hardware: start latency, fork/rollback, density. We publish a script before we publish a number.
- **Multi-node under load.** Failover, drain and migration work functionally; they have not been stress-tested. (`docs/research/`)
- Node egress decisions are not yet in the control plane's hash chain (`D80`).

## Performance
- **Userfaultfd for lazy memory restore.** Needs the host to allow it for the unprivileged VMM; today a restore merges the memory chain into a file once per checkpoint (`D81`). The first restore of each checkpoint still pays the merge.
- **Copy-on-write writable layer.** Rollback and fork copy the writable layer (ext4 has no reflink); cost grows with disk use. Fork restores clones one after another.
- Streaming `pack` for layer builds (the current fallback needs about twice the free disk).

## Product
- OOM hint for streaming and session exec (sync exec has it).
- Credential injection exercised in a real task (the mechanism exists).
- "Fork for parallel attempts" measured on a real agent task, not just on a test suite.
- Rename the remaining internal `aios` identifiers (guest paths `/aios/...`, the agent package directory `aios/`). Changing them needs rebuilt images, so it is deferred and tracked here.
- Custom VMM: deferred. A KVM VMM needs no more host privilege than Firecracker does; the cost is engineering (device models, snapshots, vsock, balloon).

## Project
- English translations of the Chinese design documents (`docs/`), starting with the user guide.
- Prebuilt guest kernel and base image as release artifacts, so a first install does not compile a kernel.
- Public CI that runs the KVM end-to-end suite on a self-hosted runner.
