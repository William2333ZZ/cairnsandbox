# Tenancy and access (Tenancy)

This page covers context 1: accounts (Account), projects (Project), members (Member) and tokens, quotas (Quota), the project default network policy, and `/me` and `/login`. The relationship is Account ⊃ Project ⊃ Member; sandboxes, templates, and volumes belong to a Project, and billing and quotas are computed per Project. For the contract see [CONTRACT §1](../../../../CONTRACT.md); for the domain design see [docs/002](../../../002-domain.md) (Chinese); for calling conventions see [README](README.md).

ID rules (accounts, Projects, and a member's `user`): lowercase letters, digits, `_`, `-`, 2–32 characters (see `tenancy.go`). Member tokens are stored only as hashes and are **returned only once** on creation and rotation.

## GET /me

Any authenticated caller.

Response:
```json
{"user": "alice", "admin": false, "auth": true, "account": "acme", "role": "owner", "projects": ["acme", "web"]}
```
`auth` indicates whether authentication is enabled on the control plane.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/me
```

## POST /login

No authentication (used by the workbench to validate a token).

Request: `{"token": "…"}`. Response: `{"ok": true, "auth": true, "user": "alice", "admin": false}`. Invalid token → **401** `{"ok": false, "user": null, …}`.

```bash
curl -s -X POST $CP/login -d '{"token":"'$TOKEN'"}'
```

## GET /accounts

Admin sees all accounts; a member sees only their own. Response `{"accounts": [{id, name, created}]}`. Error: 404 `no account` (the member's account does not exist).

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/accounts
```

## POST /accounts

Admin only. Creates an account, automatically with a Project named `default` whose id equals the account id.

Request:
```json
{"id": "acme", "name": "Acme Inc"}
```
Response **201** `{id, name, created}`. Errors: 400 invalid id or the account already exists; 403 not admin.

```bash
curl -s -X POST $CP/accounts -H "Authorization: Bearer $TOKEN" -d '{"id":"acme","name":"Acme"}'
```

## GET /accounts/\<a\>/projects

A member of this account, or admin. Response `{"projects": [{id, account, name, created}]}`. Error: 403 `not your account`.

## POST /accounts/\<a\>/projects

An owner of this account, or admin.

Request:
```json
{"id": "web?", "name": "Web"}
```
`id` may be omitted (`?` means optional; see CONTRACT §1 for the value used when omitted). Response **201** `{id, account, name, created}`. Errors: 400 invalid id or already exists; 403 `owner only` / `not your account`.

```bash
curl -s -X POST $CP/accounts/acme/projects -H "Authorization: Bearer $TOKEN" -d '{"id":"web","name":"Web"}'
```

## GET /projects/\<p\>/members

Any member of the account, or admin. Response `{"members": [{id, account, user, name, role, created}]}` (tokens not included). Errors: 404 `no project`; 403.

## POST /projects/\<p\>/members

Owner or admin. Adds a member to the account that the Project belongs to.

Request:
```json
{"user": "bob", "name?": "Bob", "role": "member"}
```
`role`: `owner` or `member`. Response **201**:
```json
{"id": "…", "account": "acme", "user": "bob", "name": "Bob", "role": "member", "token": "<appears only this once>"}
```
Errors: 400 (user invalid / already exists / role is not owner|member); 403 `owner only`; 404 `no project`.

```bash
curl -s -X POST $CP/projects/web/members -H "Authorization: Bearer $TOKEN" -d '{"user":"bob","role":"member"}'
```

## DELETE /projects/\<p\>/members/\<user\>

Owner or admin. Response `{"deleted": "bob"}`. Errors: 404 `no such member`; 403. The member's token is invalidated with it.

## POST /projects/\<p\>/members/\<u\>/rotate

An account owner, admin, or the member themselves. Generates a new token; the old token is invalidated immediately.

Request: `{"ttl_s?": 86400}` (gives the new token a lifetime, after which calls get 401; omitted means it does not expire). Response **200**:
```json
{"account": "acme", "user": "bob", "token": "<new token, appears only this once>", "expires": 1767225600.0}
```
Errors: 403 `the owner or the member themselves`; 404 `no project`; 400. Each rotation writes the audit event `member_rotate`.

```bash
curl -s -X POST $CP/projects/web/members/bob/rotate -H "Authorization: Bearer $TOKEN" -d '{"ttl_s":86400}'
```

## POST /projects/\<p\>/members/\<u\>/expiry

Same permissions as rotate. Sets a lifetime on the **existing** token.

Request: `{"ttl_s": 3600}`, `0` = does not expire. Response `{"user": "bob", "ttl_s": 3600}`. Errors: 404 `no such member`; 403. Writes the audit event `member_expiry`.

## GET /projects/\<p\>/quota

A member of the account, or admin. Response:
```json
{"project": "web", "max_sandboxes": 20, "max_storage_gib": 100, "daily_usd": 5}
```
`0` for any of the three = unlimited.

## PUT /projects/\<p\>/quota

Owner or admin. The request has the same shape as above (`project` may be omitted): `{"max_sandboxes": 20, "max_storage_gib": 100, "daily_usd": 5}`. Response: the new quota. Errors: 403 `owner only` / `not your project`; 400.

Quotas are enforced as admission checks when a **sandbox is created** (see [sandboxes.md](sandboxes.md)):

| Limit exceeded | Response |
| --- | --- |
| Number of sandboxes | 429 `{"error":"quota: sandboxes"}` |
| Storage (sum of the writable-layer allocations of this project's sandboxes + sum of volume sizes + the disk requested this time) | 429 `{"error":"quota: storage", "used_mib", "asked_mib"}` |
| Today's spend | 402 `{"error":"quota: daily budget", "project", "daily_usd", "spent_usd"}` |

```bash
curl -s -X PUT $CP/projects/web/quota -H "Authorization: Bearer $TOKEN" -d '{"max_sandboxes":20,"max_storage_gib":100,"daily_usd":5}'
```

## GET /projects/\<p\>/network

A member of the account, or admin. The Project's default network policy. Response `{"project": "web", "network": {…} | null}`.

## PUT /projects/\<p\>/network

Owner or admin. Used when a sandbox-creation request **does not carry** `network`; if it does, the request takes precedence.

Request: `{"network": {mode?, allow_hosts?, allow_direct?, rules?, limits?}}`; the policy object may also be sent directly as the body. For the shape and rule syntax see [network.md](network.md). Response `{"project": "web", "network": {…}}`. Errors: 400 invalid policy; 403 `owner only`. Writes the audit event `project_network`.

```bash
curl -s -X PUT $CP/projects/web/network -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"allowlist","allow_hosts":["pypi.org","files.pythonhosted.org"]}'
```

## DELETE /projects/\<p\>/network

Owner or admin. Removes the default; response `{"project": "web", "network": null}`.

## GET /projects/\<p\>/keys · POST · DELETE /projects/\<p\>/keys/\<id\>

API keys (D56): **named credentials** for CI, scripts, and dashboards, not human tokens. Only the project owner (and the deployer) can manage them; a key cannot itself issue further keys.

`POST` request `{"name": "ci", "role": "member" | "viewer", "ttl_s": 2592000}` (`role` defaults to member, and **there are no owner keys**; `ttl_s` omitted = does not expire). Response 201: `{"key": {id, account, project, name, role, created, expires, last_used, created_by}, "secret": "cairn_…"}` — **the secret is returned only this once**; only a hash is stored in the database.

Key behavior: it can only access the one project it belongs to (specifying another project gives 403, and listings contain only this project); `viewer` is read-only — any non-GET / HEAD request, and WebSocket upgrades (a GET that can write), return 403 `read-only credential`, and the same applies on the E2B port; it is likewise restricted when the account is suspended; `/me` shows `key: true`. `GET` lists them (without the secret, with `last_used`, updated at most once a minute); `DELETE /projects/<p>/keys/<id>` revokes immediately (the next request gets 401), and 404 if it does not exist. Audit events `key_create` / `key_revoke`.

```bash
curl -s -X POST -H "Authorization: Bearer $OWNER" -d '{"name":"ci","role":"member","ttl_s":2592000}' $CP/projects/acme-web/keys
curl -s -H "Authorization: Bearer cairn_…" $CP/sandboxes       # using the key
```

## POST /invites · GET /invites · POST /signup

Invitation-based self-service account creation (D60). The **deployer** issues a one-time invite code with `POST /invites`: `{"note": "beta first batch", "credit_usd": 5, "max_sandboxes": 3, "daily_usd": 2, "ttl_s": 604800}` (all optional; when `credit_usd` > 0 the new account automatically becomes **prepaid** and is credited this amount; the quotas are written to the new account's default project). Response 201: `{"invite": {...}, "code": "inv_…"}` — **the code is returned only this once**; only a hash is stored in the database. `GET /invites` lists them (without the code, with `used_by` / `used_at`).

**Anyone** (without a bearer) can `POST /signup` with an invite code: `{"code": "inv_…", "account": "acme", "user": "alice", "name": "Alice"}`. On success 201: `{"account": "acme", "project": "acme", "user": "alice", "token": "…", "credit_usd": 5}` — alice is the **owner** of this account, and the token is shown only once. Failures: an invalid / used / expired invite code is always 403 `invite code is not valid` (so it cannot be probed); a name that is not allowed or is already taken gives 400 (**the invite code is put back**, so you can retry with another name); at most 10 attempts per minute from the same source address, beyond which 429. Audit events `invite_create` / `signup`.

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" -d '{"note":"wave 1","credit_usd":5}' $CP/invites
curl -s -X POST -d '{"code":"inv_…","account":"acme","user":"alice"}' $CP/signup      # no token needed
```

The console login page has a "Have an invite code? Create an account" entry; after creation it shows the token directly and logs you in.

## DELETE /accounts/\<a\>

Deployer only. **Take an account offline** (D68): deletes its members, API keys, projects (together with quotas and default network), and its suspension and billing-mode records. If the account still has sandboxes or volumes, it returns **409** (with the counts); adding `?force=1` deletes the sandboxes and volumes as well. **Retained**: usage events, the credit ledger, settled statements, and the audit log (these are accounting evidence). The account name is **reserved and not reissued** (the ledger and usage are recorded by name, so the next person to register the same name would inherit them). The default account cannot be deleted. Audit event `account_delete`. Response 200: `{"account": "acme", "deleted": true, "sandboxes_deleted": 2, "volumes_deleted": 1, "removed": {"members": 3, "api_keys": 1, …}, "kept": "…"}`.

## POST /accounts/\<a\>/suspend

Deployer only. Suspends an account (abuse handling, D53). Request `{"reason": "…"}`, **reason is required** (the account's members will see it). Effects: (1) the suspension is recorded; (2) all **running** sandboxes in all of the account's projects are paused (state preserved, compute and egress stop); (3) from then on the account's members can only **view** (the sandbox list and single-sandbox info, plus its checkpoint / egress / metrics sub-items) and **`DELETE`** to clean up — they cannot create, resume, exec, or change the network, **nor read files, events, ports, or open a WebSocket** (the node would automatically resume a paused sandbox on these requests, so allowing them would amount to lifting the pause); the E2B port is the same; these return 403 `account suspended`; (4) `/me` gains a `suspended` field, and the console shows a red banner at the top. The deployer is not restricted (to ease investigation).

Response 200: `{"account": "acme", "suspended": true, "reason": "…", "paused": ["sb-…"], "pause_failed": []}`. Account does not exist: 404. Audit event `account_suspend`.

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" -d '{"reason":"crypto mining, see ticket 123"}' $CP/accounts/acme/suspend
```

## POST /accounts/\<a\>/unsuspend

Deployer only. Lifts the suspension; it does **not** automatically resume the paused sandboxes — the owner decides which to resume. 404 if the account is not suspended. Audit event `account_unsuspend`.

## GET /suspensions

Deployer only. `{"suspensions": [{account, reason, by, at}]}`. Metric `cairn_accounts_suspended`.

## The legacy /users

The multi-user endpoints from contract §8, equivalent to members of the `default` account. Admin only.

| Endpoint | Description |
| --- | --- |
| `GET /users` | `{"users": […]}` |
| `POST /users {user, name}` | **201** `{user, name, token}`, the token is returned only once; 400 on validation failure |
| `DELETE /users/<user>` | `{"deleted": "<user>"}`; 404 `no such user` |

New code should use `/projects/<p>/members`.
