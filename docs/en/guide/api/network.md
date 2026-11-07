# Network and Egress (Network)

This document covers context 6: the shape and rule syntax of sandbox egress policy, the credential vault on nodes, and the request queue for "loosening needs approval". Enforcement (nftables, the egress proxy, the audit ring) happens on the node; the control plane only validates the shape of the policy at the edge and pushes credentials to every node. For the contract see [CONTRACT §1](../../../../CONTRACT.md), for the domain design see [docs/002 (Chinese)](../../../002-domain.md), and for the threat model see [docs/013 (Chinese)](../../../013-threat-model.md). Endpoints that set the policy: `network` in sandbox creation, `PUT /sandboxes/<id>/network` ([sandboxes.md](sandboxes.md)), `PUT /projects/<p>/network` ([tenancy.md](tenancy.md)). For calling conventions see the [README](README.md).

## Policy and rule syntax

```json
{
  "mode": "allowlist",
  "allow_hosts": ["pypi.org", "*.pythonhosted.org"],
  "allow_direct": [],
  "rules": [
    {"name": "gh-read", "match": {"scheme": "http", "host": "api.github.com", "method": ["GET"], "path": "/repos/*"},
     "action": {"allow": true, "inject": "github", "upstream": "https"}}
  ],
  "limits": {"requests_per_min": 600, "max_bytes": 104857600}
}
```

| Field | Meaning |
| --- | --- |
| `mode` | `closed` (deny all by default) / `allowlist` (according to `allow_hosts`) / `audited` (allow and record everything). Any other value → 400 |
| `allow_hosts` | List of strings; when no rule matches, audited allows, otherwise only the hosts listed here are allowed (empty = deny all) |
| `allow_direct` | List of strings; direct-connect addresses |
| `rules` | In order, **the first match wins**; each rule is described in the table below |
| `limits` | `{requests_per_min?, max_bytes?}`, non-negative integers, any other key → 400. Per-sandbox caps on requests per minute and cumulative bytes through the egress proxy; when exceeded the proxy replies 429 (audit `egress_limited`) |

Rules:

| Field | Values |
| --- | --- |
| `name` | Matches `[a-z0-9][a-z0-9_.-]{0,39}`; if omitted, it is `rule<i>` |
| `match.scheme?` | `http` or `https` |
| `match.host?` | `"exact"`, `"*.suffix"`, a glob, or `"~regex"` (starts with `~`, **matches the whole name, case-insensitive**) |
| `match.method?` | List of methods, e.g. `["GET","POST"]` |
| `match.path?` | `"/exact"` or `"/prefix/*"`, must start with `/` |
| `action.allow` | **Required**, bool |
| `action.inject?` | Name of a credential in the vault; adds that credential's header to the **plaintext HTTP** request, and the value never enters the sandbox. Can only be used on rules with `allow:true` |
| `action.upstream?` | Only `"https"` (D89): the sandbox sends plain `http://host/…`, the proxy adds the credential and opens TLS to the server itself (port 443 by default, certificate always verified; a private CA goes in the node's `EGRESS_CA_FILE`). Only on rules with `allow:true` and `match.scheme` `http` |

`method` and `path` take effect only for plaintext HTTP; for an HTTPS CONNECT tunnel only the host and port are visible. To inject a credential into an HTTPS API (the example above), have the program in the sandbox use `http://` for that host and write the rule with `scheme: http` plus `upstream: "https"`; the key only travels inside the proxy's TLS connection to the server. A bad rule returns 400 on both the control plane and the node.

## Hint when denied

When the egress proxy rejects a request, the reason phrase in the HTTP status line is a single actionable sentence (Go's `go install` and curl both print it):

```
403 egress denied for goproxy.cn by the sandbox network policy; to allow it: PUT /sandboxes/sb-123/network {"allow_hosts":["goproxy.cn"]}
```

A member sending this PUT gets a 202 and a pending request, which takes effect once an owner / admin approves it (see below). (D79)

## What "loosening" means

A member's request to `PUT /sandboxes/<id>/network` counts as loosening if it meets any of the conditions below relative to the sandbox's **current** policy; it then does not take effect immediately but goes into approval (the 202 flow in [sandboxes.md](sandboxes.md)). The decision is made by `network.Relaxes` (`cp/internal/network/requests.go`):

| Loosening | Example |
| --- | --- |
| A looser `mode` (`closed` < `allowlist` < `audited`) | allowlist → audited |
| `allow_hosts` / `allow_direct` contains an item the current policy does not have | Adding `github.com` |
| A new allow rule (the current rules do not contain one deeply equal to it) | Adding an `allow:true` rule |
| Removing a deny rule (an `allow:false` rule in the current rules is missing from the new `rules`) | |
| Raising or removing `limits` (a currently set cap, with a new value that is larger or 0) | `requests_per_min` 600 → 6000 |

Keys that are not sent are not compared (the node merges by key). Tightening (everything else) takes effect immediately. Requests from owners and admins do not go through approval.

## GET /network/requests

Caller. Lists loosening requests for the account's Projects; admin sees all. By default only `pending` ones are returned; `?all=1` (any non-empty value) also returns decided ones. Newest first.

Response:
```json
{"requests": [{"id": "nr-…", "sandbox": "sb-123", "project": "web", "by": "bob", "network": {…}, "created": 1767225600.0, "status": "pending"}]}
```
Decided ones additionally have `decided_by` and `decided`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/network/requests?all=1"
```

## POST /network/requests/\<id\>/approve

Project owner or admin. The requested policy is pushed to the node (`PUT /sandboxes/<id>/network`) only upon approval; if the node refuses, the node's status code is returned as is with `{"error":"the node refused the policy","detail":…}`, and the request remains pending. Success response `{"request": {…, "status": "approved"}}`, and the audit `network_relax_approved` is written.

Errors: 403 `an owner of the project decides`; 404 `no such request`; 409 `already approved|denied`.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/network/requests/nr-abc123/approve
```

## POST /network/requests/\<id\>/deny

Same permissions as above. Does not touch the node. Response `{"request": {…, "status": "denied"}}`, audit `network_relax_denied`. Already decided → 409.

## Credential vault (admin only)

Credential **values exist only on nodes** (`state/credentials.json`, 0600, or `EGRESS_CREDENTIALS_FILE`); the control plane pushes values to every live node and keeps none, and the audit records only names. A credential name matches `[a-z0-9][a-z0-9_.-]{0,39}`, and a header name contains only letters, digits, and `-`.

### GET /network/credentials

Response `{"nodes": {"node-a": ["github", "openai"], "node-b": ["github"]}}` -- which node has which names (a node that joined later may lack some). Non-admin → 403.

### PUT /network/credentials/\<name\>

Request:
```json
{"header": "Authorization", "value": "Bearer ghp_…"}
```
Response `{"name": "github", "header": "Authorization", "nodes": {"node-a": "ok", "node-b": "unreachable: …"}}`. Errors: 400 (invalid name / header, empty `value`); 403. Afterwards use it by writing `"inject": "github"` in a rule.

```bash
curl -s -X PUT $CP/network/credentials/github -H "Authorization: Bearer $TOKEN" \
  -d '{"header":"Authorization","value":"Bearer ghp_xxx"}'
```

### DELETE /network/credentials/\<name\>

Response `{"name": "github", "nodes": {"node-a": "ok"}}` (a node that failed shows `"failed"`).

## GET /sandboxes/\<id\>/egress

The most recent 200 egress decisions, see [sandboxes.md](sandboxes.md).
