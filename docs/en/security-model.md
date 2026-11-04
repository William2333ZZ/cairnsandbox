# Security model

The full threat model is `docs/013-threat-model.md` (Chinese). This page is the summary an outside reviewer needs.
**It has not been reviewed by a third party.** Treat every claim here as ours.

## Boundaries

1. **Guest to host.** Each sandbox is a Firecracker microVM with its own kernel. The VMM runs under the jailer: chroot, an unprivileged uid per VM, a per-VM cgroup (memory, CPU, pids).
   The guest kernel is trimmed (no loadable modules, no io_uring, no user namespaces); inside the guest, agent code runs as an unprivileged user with seccomp and Landlock available per command.
2. **Sandbox to network.** No direct network and no DNS from the guest. All outbound traffic goes through the node's proxy and the sandbox's host allowlist; each decision is logged.
   Loosening a policy as a project member requires an owner's approval. Credentials can be injected at the proxy so the sandbox never holds them.
3. **API caller to control plane.** Bearer tokens (hashed at rest, expiry, rotation, revocation), roles (owner / member / viewer), named single-project API keys, per-project quotas and rate limits.
4. **Control plane to node.** A node token; nodes fence themselves (stop their own sandboxes) if the control plane is silent for a configured time.
5. **Audit.** Control-plane actions, including `exec` and file operations, are written to a hash-chained log that can be verified with `GET /audit/verify`.
   Command lines are redacted for common secret shapes and truncated; that is a net, not a guarantee.

## Known gaps

* No external penetration test or audit.
* The node container is privileged by necessity (devices, `SYS_ADMIN`, `SYS_PTRACE`); a node compromise exposes that node's sandboxes. One node per trust domain is the safe deployment.
* The node's egress decision log is not yet part of the control plane's hash chain.
* Multi-node failover and TLS between control plane and node have been exercised functionally, not under load or adversarially.
* Redaction of secrets in audited command lines is pattern-based.

## Reporting

See `SECURITY.md`.
