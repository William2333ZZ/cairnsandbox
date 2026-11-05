# Roadmap

Honest list of what is missing, in rough priority order. Each item names where the reasoning lives. Nothing here is a promise of a date.

## Trust (blocks wider use)
- **Independent security review** of isolation, egress policy and tenancy. None yet. (`SECURITY.md`, `docs/013-threat-model.md`)
- **Baselines against other sandboxes** on the same hardware: start latency, fork/rollback, density. We publish a script before we publish a number.
- **Multi-node under load.** Failover, drain and migration work functionally; they have not been stress-tested. (`docs/research/`)
- Node egress decisions are not yet in the control plane's hash chain (`D80`).

## E2B compatibility
What the official SDKs can and cannot do against CairnSandbox is in `docs/guide/api/e2b.md` (a matrix, in Chinese) and `docs/en/e2b.md`. The open gaps:
- **Template builds are partial.** Steps are `RUN`, `COPY`, `ENV`, `WORKDIR`, `USER` plus a start command and ready command. The base is one of our own images or templates; `from_image` names such as `ubuntu` or `debian` map to our bases and **nothing is pulled from a registry** (other images are rejected; build an OCI layer through the native API first). No template tags. A build sandbox uses the project's default egress policy, which denies everything, so `pip install` or `apt-get` steps fail until the owner opens it. The SDK helpers `pip_install` / `apt_install` / `git_clone` expand to `RUN` and have not been tested one by one.
- **Port URLs (`get_host`) need a preview domain.** They work only when you set `PREVIEW_DOMAIN` and point wildcard DNS at the control plane, over cleartext HTTP unless you terminate TLS yourself, behind a traffic token. There is no TLS or DNS handling in CairnSandbox itself, and no WebSocket on the port proxy.
- **Template COPY uploads are replica-local.** Uploaded context files go to a directory on one control plane replica (`CP_E2B_UPLOAD_DIR`); with several replicas, the upload and the build trigger can reach different ones and the build fails. A fix is to store uploads in the shared object store.
- **A committed template lives on the node that built it.** Creating from it elsewhere needs the layer to be copied; today a create goes to a node that holds it, and returns 429 when those are full.
- Not provided: `StreamInput` client streaming (the SDK's `send_stdin` does not need it), `/init`, `/freeze`, sandbox `/logs`, team and key management, rule-based header rewriting in `update_network`, `autoPause`/`volumeMounts` on create (ignored).
- Verified only with the Python and npm SDKs (2.52 to 2.63), `e2b-code-interpreter` 2.10 to 2.11, on one host with two nodes; no Go or Java SDK; no load test. The 1.x SDKs cannot be pointed at a custom API URL and are refused by our suites.
- code-interpreter: no structured `chart` object, no IPython magics, no R or Java kernels; third-party Python packages come only from the `ci-pylibs` layer you build.

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
