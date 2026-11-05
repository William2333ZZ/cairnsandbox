**What and why**

**Which layer** (sandbox cloud: `cp/ node/ guest/ sdk/ console/ deploy/ recipes/` · agent layer: `aios/ intake/ eval/ web/`). The agent layer may reach the sandbox cloud only through its HTTP API or the SDK.

**How it was tested** (unit tests, and whether the KVM end-to-end suite was run if `node/` or `guest/` changed; the E2B conformance suite if `cp/internal/e2b/` or `guest/sbx-runtime/src/envd.rs` changed)

- [ ] Commits are signed off (`git commit -s`)
- [ ] No secrets, tokens, hostnames or personal paths in the diff
- [ ] A decision record and `docs/decisions-index.md` line if an interface or guarantee changed
- [ ] `CHANGELOG.md` line if the change is visible to users
- [ ] A new third-party dependency or bundled file is listed in `NOTICE` with its license
