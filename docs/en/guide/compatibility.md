# Versioning and Compatibility Commitments

**Current state**: no stable release with a version number has been published yet; what follows is what we **intend** to do, and the gap between that and today is written at the end. The interface contract is [CONTRACT.md](../../../CONTRACT.md), and changes are recorded in the [CHANGELOG](../../../CHANGELOG.md).

## Version numbers

- The sandbox cloud (control plane + nodes + SDK) uses one version number, `MAJOR.MINOR.PATCH`; the SDK version follows it (`cairnsandbox.__version__` is currently 0.1.0).
- The agent layer is numbered separately and does not affect the sandbox cloud's commitments (there is only one seam between the two layers, 002).
- During 0.x the interface may change, but every change goes into the CHANGELOG under "Known incompatibilities"; from 1.0 the rules below apply.

## Commitments after 1.0

| Change | Rule |
| --- | --- |
| Adding an endpoint, adding an optional request field, adding a response field | MINOR, not a breaking change; **callers must ignore response fields they do not recognize** |
| Changing a field's meaning, changing a status code, removing an endpoint or field, making an optional field required | MAJOR |
| Deprecation | Marked first in the CHANGELOG and the `Deprecation` response header, and kept for at least one MINOR before removal |
| Error message text | Not guaranteed; decide by status code and the `error` prefix |
| Audit and `/metrics` fields | Additions only, no changes; once published, a metric name does not change |
| E2B-shaped endpoints | Follow E2B's REST; if E2B changes we follow, and fields we cannot follow are listed in the compatibility matrix ([api/e2b.md](api/e2b.md)) |
| Node interface (contract §2) | Internal interface; control plane and nodes are deployed at the same version; no external commitment |
| Upgrade order | Nodes first, then the control plane, then the SDK; the control plane is forward compatible with nodes of the previous MINOR |

## Gap to today

- There is no release process, no tags, and no `Deprecation` header;
- Fields in the contract are not yet marked "stable / experimental";
- There are no compatibility tests (using the previous SDK version against the current control plane).

Once these three are done, 1.0 can be announced.
