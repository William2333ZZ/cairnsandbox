# Webhooks (Project Event Push)

Events that happen in a project are **pushed** to your HTTPS address (D72), so you do not need to poll. Only the project owner (and the deployer) can manage them; API keys and members cannot. For calling conventions see the [README](README.md).

## Events

| Event | When | `data` |
| --- | --- | --- |
| `sandbox.created` | A sandbox was created successfully | `sandbox`, `node` |
| `network.approval_requested` | A member requested to loosen a sandbox's egress, awaiting owner approval | `sandbox`, `request` |
| `build.finished` | A build ended (success or failure) | `build`, `kind`, `ok`, `error` |
| `account.suspended` / `account.unsuspended` | The account was suspended / reinstated (including credit exhausted) | `account`, `reason` (on suspension) |
| `account.credit` | A top-up or adjustment was recorded for the account | `account`, `usd` |
| `ping` | You clicked "test" | `message` |

Account-level events are sent to the webhooks that subscribe to them in **each project** of that account.

## Registration

`POST /projects/<p>/webhooks`, with request `{"url": "https://hooks.example.com/cairn", "events": ["sandbox.created", "build.finished"]}` (`events` omitted or empty = all). Response 201: `{"webhook": {id, url, events, …}, "secret": "whsec_…"}` -- **the signing secret is returned only this once**.

- The URL must be **https**, must not contain a username or password, and must not be a private / loopback / link-local address or `localhost`. **The real protection is at connection time**: after resolving the domain name, the control plane checks the IP it will actually connect to and rejects private, loopback, link-local, carrier-grade NAT (100.64/10), multicast, and reserved addresses -- so domains pointing at the internal network and DNS rebinding do not get through; redirects are not followed.
- At most 20 per project.
- `GET /projects/<p>/webhooks` lists them (without secrets; with `failures`, `last_status`, `last_at`, `disabled`); `DELETE /projects/<p>/webhooks/<id>`; `POST …/<id>/test` sends a `ping`; `POST …/<id>/enable` re-enables one that was automatically disabled.

## Delivery

A `POST` to your address with `Content-Type: application/json` and these headers:

| Header | Value |
| --- | --- |
| `X-Cairn-Event` | The event name |
| `X-Cairn-Delivery` | The id of this delivery (`dl-…`), unchanged across retries, usable for deduplication |
| `X-Cairn-Signature` | `sha256=` + hex of `HMAC-SHA256(secret, raw request body)` |

Request body: `{"id": "dl-…", "type": "sandbox.created", "project": "acme", "created": 1767225600.5, "data": {…}}`.

**Verifying the signature** (Python):
```python
import hmac, hashlib
def verify(secret: str, body: bytes, header: str) -> bool:
    return hmac.compare_digest(header, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
```
Verify against the **raw bytes**; do not parse and then re-serialize.

**Reliability**: a 2xx counts as success; otherwise it retries immediately, then once more after 5 seconds and once more after 30 seconds, 3 attempts in total; if all fail, one failure is recorded. **10 consecutive failures automatically disable** the webhook (`disabled: true` in the list); `enable` it after fixing the problem. There are **no** guarantees beyond at-most-once semantics: events can be lost when the control plane restarts, when the queue is full (1024 entries), or with multiple replicas -- to reconcile, use [`GET /audit`](ops.md) or the list endpoints, and do not treat webhooks as the only source. Events come only from audit rows written by this replica, and each audit row is delivered only once, by the replica that wrote it.
