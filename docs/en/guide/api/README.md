# HTTP API Reference

This directory is the HTTP interface reference for the CairnSandbox sandbox cloud control plane (fc-cp). It is split into files by the eight contexts of [docs/002 (Chinese)](../../../002-domain.md), with one section per endpoint: who can call it, request, response, errors, and a `curl` example. Fields and status codes follow [CONTRACT.md](../../../../CONTRACT.md) §1 / §1a and `cp/internal/httpapi/server.go`; where the contract is unclear, this document says "see CONTRACT §1" instead of guessing. The node-layer interface (CONTRACT §2) is internal; the control plane and nodes are deployed at the same version, so it is not covered here.

## How to call

| Item | Description |
| --- | --- |
| Base URL | The control plane defaults to `http://127.0.0.1:18700` (`http://fc-cp:8700` inside the container). The `curl` examples below use `$CP` for it and `$TOKEN` for your token |
| Authentication | `Authorization: Bearer $TOKEN`. Required everywhere except `/health`, `/ui`, and `/login`. The server also accepts a `?token=` query parameter (see server.go), but tokens end up in logs and history, so do not use it |
| CP_TOKEN not set | Per the contract: when it is not set, no check is made. Every deployment should set it |
| Project | Requests for resources that belong to a Project (creating sandboxes, volumes, builds, etc.) specify it with the `X-Project: <id>` header, the `project` field in the body, or `?project=<id>`; if none of the three is given, the caller account's `default` is used. A Project of another account → 403; nonexistent → 404 |
| Data format | Request and response bodies are both JSON (`Content-Type: application/json`). Exceptions: file `PUT`/`GET` (raw bytes), request bodies of `/builds/archive` and `/volumes/from-archive` (tar), `exec/stream` (line-delimited JSON), `/metrics` (Prometheus text), `/events/stream` (SSE), and billing `?format=csv` |
| Errors | `{"error": "…"}` together with an HTTP status code. A few errors carry extra fields (e.g. a quota 429 carries `used_mib`). Error text is not guaranteed to be stable; decide by status code and the `error` prefix. The error body of the E2B port (the second port) is `{code, message}`, see [e2b.md](e2b.md) |

### Status codes

| Code | Meaning |
| --- | --- |
| 200 | Success |
| 201 | Created (sandbox, volume, account, member, build, checkpoint, settlement, inbox entry) |
| 202 | Accepted but not in effect: a member loosened a sandbox's network policy and it awaits owner approval ([sandboxes.md](sandboxes.md)) |
| 400 | Bad request: invalid JSON, field validation failure (network rules, priority, overlapping settlement periods, etc.) |
| 401 | Token invalid, expired, or rotated out |
| 402 | Today's spending has reached the Project quota limit (`quota: daily budget`); the model proxy also uses 402 when over budget (see contract §4) |
| 403 | Insufficient role (`deployer only` / `owner only`) or not your account / Project |
| 404 | Does not exist, or exists but belongs to another account (invisible to you) |
| 409 | Conflict: volume still attached, checkpoint name already taken, network request already decided, volume and node mismatch, etc. |
| 413 | Request body exceeds `CP_MAX_BODY_MIB` |
| 429 | No node has capacity; quota (sandbox count, storage) reached; non-admin token over rate limit (with `Retry-After`) |
| 502 | Node or backend service unreachable; nothing is listening on the exposed port |
| 503 | Node is down or draining; `/eval`, `/intake` have no backend configured |

## Roles

| Role | Who | What they can do |
| --- | --- | --- |
| deployer / admin | The deployer holding `secrets/cp.token` (i.e. `CP_TOKEN`) | Everything; is admin of all accounts. Nodes, migration, price list, template / layer distribution, the credential vault, `/metrics`, etc. can only be called by this role |
| account owner | A member with `role: owner` in an account | Within their own account: create Projects, add and remove members, change quotas and the project default network, approve network loosening, settle bills |
| member | A member with `role: member` in an account | The account's sandboxes, templates, volumes, builds, billing (read), audit (read); tightening the network takes effect immediately, loosening requires approval |

Path prefixes a member can access (`memberPaths`): `/intake`, `/health`, `/me`, `/eval/versions`, `/eval/evolve`, `/sandboxes`, `/templates`, `/accounts`, `/projects`, `/metering`, `/volumes`, `/builds`, `/layers`, `/network/requests`, `/audit`, plus `/events/stream`. All other paths return `403 {"error":"deployer only"}` to members. Within these prefixes, a few endpoints still require owner or admin; each section says so.

## Limits

| Limit | Value | When exceeded |
| --- | --- | --- |
| Request body size | `CP_MAX_BODY_MIB`, default 512 MiB (image archives and volume tars go in the request body) | 413 |
| Rate | `CP_RATE_PER_MIN`, default 600 requests / minute / non-admin token (token bucket); set 0 to disable; admin is unlimited | 429 `{"error":"rate limit","per_min":…}` with `Retry-After: 1` |
| Single file | 1 GiB (`/sandboxes/<id>/files`) | See [sandboxes.md](sandboxes.md) |

## Contents

| File | Context ([docs/002 (Chinese)](../../../002-domain.md)) | Content |
| --- | --- | --- |
| [tenancy.md](tenancy.md) | 1 Tenancy and access | Accounts, Projects, members and tokens, quotas, project default network, `/me`, `/login` |
| [sandboxes.md](sandboxes.md) | 2 Sandbox lifecycle, 8 Execution plane | Create / query / pause / ttl / exec / sessions / files / fork / checkpoints / network / ports / migration / export |
| [state.md](state.md) | 5 State | Volumes, bundle storage; checkpoints are in sandboxes.md |
| [images.md](images.md) | 4 Images and templates | Templates, layers, builds (oci / commit / archive), distribution |
| [network.md](network.md) | 6 Network and egress | Credential vault, loosening approval, rule syntax |
| [metering.md](metering.md) | 7 Metering and billing | Price list, bills, settlement, usage |
| [webhooks.md](webhooks.md) | 1 Tenancy and access | Project event push: registration, signing, retries, event table |
| [ops.md](ops.md) | 3 Nodes and capacity | Nodes, fleet, routes, `/metrics`, `/audit`, event stream (admin) |
| [e2b.md](e2b.md) | E2B-shaped port | Second port, route mapping, compatibility matrix |

Other: `GET /health` (no authentication) → `{ok, replica, registry:{kind, ok, where}, nodes:{name:{alive, running, total, mem_free_mib, last_err}}, routes}`; `/eval/<path>` and `/intake/<path>` forward the request to the evaluation service and the intake service (prefix stripped, with the control plane's bearer and `X-User`); their interfaces are in CONTRACT §3, §8 and are outside this reference. For versioning and compatibility commitments see [../compatibility.md](../compatibility.md).

```bash
export CP=http://127.0.0.1:18700
export TOKEN=$(cat secrets/cp.token)
curl -s $CP/health
curl -s -H "Authorization: Bearer $TOKEN" $CP/me
```
