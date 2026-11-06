# Going to Production: Reverse Proxy, Domains, Environment Variables, and Launch Checklist

Written for people taking the sandbox cloud from "running under compose on one machine" to "serving the public". For the design rationale see the checklist at the end of the [013 threat model](../../013-threat-model.md) (Chinese) and the [commercialization gap](../../research/commercialization-gap-2026-11.md); for day-to-day operations see the [operations manual](operations.md). **Where this page says "not verified", it means we have not exercised it in a real multi-machine / public-internet environment.**

## 1. Topology

```
 User ──HTTPS──▶ reverse proxy (Caddy / nginx) ──▶ control plane :18700 (API, console /console/, /status)
                         │                         └▶ E2B endpoint :18710
                         └─ *.preview.example.com ─▶ control plane :18700 (rewritten by Host to /sandboxes/<id>/ports/<port>)
 Control plane ──HTTPS (optional)──▶ node :8800 (one per machine) ──▶ microVM
 Control plane ──▶ Redis (routing)  Postgres (tenants, ledger, usage)  fc-store (blocks, can land on Garage S3)
```

The control plane and node ports bind only to 127.0.0.1 / the internal network by default; **expose only the reverse proxy's 443 externally**.

## 2. Caddy example (automatic HTTPS)

```caddyfile
api.example.com {
    reverse_proxy 127.0.0.1:18700 {
        flush_interval -1          # SSE (/events/stream) and streaming exec must not be buffered
    }
}
e2b.example.com {
    reverse_proxy 127.0.0.1:18710
}
# Preview domain: a wildcard certificate needs DNS validation (Caddy's DNS plugin, or your own certificate)
*.preview.example.com {
    tls { dns <your DNS provider plugin> {env.DNS_TOKEN} }
    reverse_proxy 127.0.0.1:18700 {
        flush_interval -1
    }
}
```
On the control plane set: `PREVIEW_DOMAIN=preview.example.com`, `CP_TRUST_PROXY=1` (set it only when you really have your own reverse proxy in front; it is used for per-source-address rate limiting on the registration endpoint). Caddy passes WebSocket upgrade headers through by default. 〔Not verified: preview and cookie behavior on a public domain (`Secure` is decided from `X-Forwarded-Proto`, which Caddy sends)〕

## 3. Environment variable reference (control plane)

| Variable | Purpose | Default |
| --- | --- | --- |
| `CP_TOKEN` / `CP_TOKEN_FILE` | Deployer token | None (= no authentication, **development only**) |
| `CP_REGISTRY` | `redis://…` shared routing for multiple replicas | File (single replica) |
| `CP_DB` | `postgres://…`; empty = SQLite | SQLite |
| `NODES` | Node list as JSON; can also be `POST /nodes` | `[]` |
| `NODE_CA_FILE` | CA used to verify https nodes (D62) | System root certificates |
| `STORE` | fc-store address | Empty (no cross-node state) |
| `CP_MAX_BODY_MIB` / `CP_RATE_PER_MIN` | Request body limit / requests per token per minute | 512 / 600 |
| `CP_AUDIT_EXEC` | `0` = do not record exec and file reads/writes in the audit chain (D80; recorded by default, with command lines redacted and truncated to 200 characters) | 1 |
| `CP_TRUST_PROXY` | Trust `X-Forwarded-For` (corrected after D56) | 0 |
| `PREVIEW_DOMAIN` | Preview domain (D55) | Empty (disabled) |
| `AUTO_EVACUATE_AFTER_S` | How long after a node is lost before automatic takeover (D54); **must be greater than the node's `FENCE_AFTER_S` + 30** | 0 (off) |
| `ABUSE_CPU_PCT` / `ABUSE_EGRESS_BYTES_PS` / `ABUSE_WINDOW_S` / `ABUSE_SCAN_S` | Abuse detection thresholds (D58); CPU 0 = off | 90 / 0 / 600 / 60 |
| `CONSOLE_DIR` | Directory of console static files | `/console` |

Node: `TLS_CERT` / `TLS_KEY` (HTTPS), `FENCE_AFTER_S` (self-fencing), `MAX_VMS`, `MEM_BUDGET_MIB`, `METRICS_EVERY_S` / `METRICS_POINTS`, `JAIL` (must be 1 in production). Chunk store: `STORE_BACKEND=s3` and `S3_*` (see the storage backend section of the [operations manual](operations.md)).

## 4. Pre-launch checklist (by importance)

**Required**
1. `CP_TOKEN` is set, and the `secrets/` directory has permissions 0700 and is not in git.
2. Nodes run with `JAIL=1`; the machines the nodes run on are **dedicated** (a VM escape lands in the privileged node container, see 013 T1).
3. The reverse proxy has TLS; the ports of the control plane / nodes / Redis / Postgres are not bound to the public internet.
4. Enable TLS between control plane and nodes when they are on different machines (`deploy/tls/gen-certs.sh`, D62), and check certificate expiry reminders.
5. Postgres and Redis are backed up and the **restore has been drilled** (Postgres and Garage have been drilled, `deploy/garage/restore-drill.sh`; the local-directory `store/` has not been drilled).
6. Alert rules ([alerts.yml](../../../deploy/alerts.yml)) are wired to somewhere someone is watching, at least `AiosNodeDown`, `AiosStoreDown`, `AiosServerErrors`, `AiosAbuseSignal`.

**Before opening to strangers**
7. Get an external penetration test done (013 T1); fix the findings before opening.
8. Drill node fencing and automatic takeover (D54) on a machine that can be disconnected from the network, and turn on `AUTO_EVACUATE_AFTER_S` only after confirming the window is set sensibly.
9. Set the price list from real costs (see the [pricing comparison](../../research/pricing-2026-11.md)), make accounts prepaid by default (D59), and give invitation codes a trial credit and quotas (D60).
10. Put the status page somewhere **independent of the control plane** (it must not go down along with the control plane): a static page polling `/status`, or a third-party uptime service that judges by status code (503 = outage).
11. Write the SLA / privacy / data processing terms (this is not an engineering problem, but you should not take money without them).

## 5. Dependencies and vulnerabilities (D73)

Run this by hand before launch and once a month afterwards (the box's egress is restricted, so it is not in CI):

```bash
cd cp && go run golang.org/x/vuln/cmd/govulncheck@latest ./...        # control plane; if there is an "affected by", upgrade the module and rebuild the image
cd console && npm audit --omit=dev; cd ../sdk/js && npm audit         # front end and JS SDK
```
**Running the control plane scan inside a sandbox** (D78, no need to install Go on the box): `MEM=4096 python3 ops/dogfood-egress.py <source tar> dev-go` (internally it cold-starts the template's layers at 4 GiB using `template_key` + `boot:"cold"` + `mem`). Sandbox egress is fully closed by default, so the first run is rejected; request and approve the domains named in the failure (`goproxy.cn`, `vuln.go.dev`), and after two rounds you get the result. Memory must be 4 GiB (peak about 1.9 GB; the 2 GiB dev template gets OOM-killed, exit code 137).

Base images (`alpine:3.20`, `ubuntu:22.04`, `golang:*-alpine`) should also be re-pulled and rebuilt regularly.

## 6. Upgrade and rollback

See ["Upgrade" in deploy/README](../../../deploy/README.md): when a node receives SIGTERM it pauses running persistent sandboxes to disk before exiting and resumes them once the new container is up; the control plane is stateless and can simply be swapped; with multiple replicas, roll them. Rollback = switch back to the old image tag. The database only adds tables and columns (idempotent migrations), so an old control plane version can run against a new database (ignoring the new tables).
