# Operations (nodes, capacity, observability)

This page covers context 3 (nodes and capacity, Fleet) and the control plane's observability surface: node registration and draining, load balancing, the route table, `/metrics`, the audit log and its verification, the entry points for template / layer distribution, sandbox import, and the change event stream. Unless stated otherwise these are **admin only** (deployer token); members get `403 {"error":"deployer only"}`. For the contract see [CONTRACT §1](../../../../CONTRACT.md); for the domain design see [docs/002](../../../002-domain.md) (Chinese); for deployment see [deploy/README](../../../../deploy/README.md). For calling conventions see [README](README.md).

## GET /health

No authentication. Response:
```json
{"ok": true, "replica": "cp-1", "registry": {"kind": "file", "ok": true, "where": "…"},
 "nodes": {"node-a": {"alive": true, "running": 3, "total": 5, "mem_free_mib": 20480, "last_err": null}}, "routes": 12}
```
With multiple replicas, each replica reports its own `replica`; `registry` is the registry they share (`file` or `redis`).

## GET /nodes

Admin only. Response `{"nodes": [{name, url, alive, health:{vms_total, vms_running, max_vms, mem_mib_running, mem_budget_mib, template_snapshots, pools, …}, load, …}]}`. `load` is the larger of the slot-usage ratio and the memory-usage ratio.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/nodes
```

## POST /nodes

Admin only. Registers a node and writes it to the registry; other replicas recognize it at their next poll.

Request:
```json
{"name": "node-b", "url": "http://10.0.0.2:8701", "token": "…", "token_file": "…", "reserve_vms?": 2, "reserve_mem_mib?": 1024}
```
Use either `token` or `token_file`. `reserve_vms` / `reserve_mem_mib` is the headroom this node keeps for interactive work; `0` = the global default (2 VMs / 1 GiB), and batch / background work is only placed on nodes that still have this much headroom left. Response **201** `{"added": "node-b"}`. Error: 400 `name and url required`.

```bash
curl -s -X POST $CP/nodes -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"node-b","url":"http://10.0.0.2:8701","token_file":"/run/secrets/node-b.token","reserve_vms":2,"reserve_mem_mib":1024}'
```

## POST /nodes/\<n\>/cordon · uncordon

Stop placing new sandboxes / resume. The flag lives in the registry and is consistent across replicas; creations pinned with an explicit `node:` are unaffected. Response `{"node": "node-a", "cordoned": true}`. Error: 404 unknown node.

## POST /nodes/\<n\>/drain

Cordons first, then moves every **persistent** sandbox on it elsewhere (done by one replica holding the lease). Response:
```json
{"node": "node-a", "cordoned": true, "moved": ["sb-1"], "left": ["sb-9"], "failed": []}
```
`left` are ephemeral sandboxes (not moved), `failed` are those that could not be moved. A creation that lands on a draining node → 503.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/nodes/node-a/drain
```

## POST /nodes/\<n\>/evacuate

Admin only. Takeover after a node has died: imports the durable sandboxes on it to other live nodes from their last published bundle (done by one replica at a time, holding the lease); non-durable ones are counted as lost; **volumes are not carried** (they are on the dead node's disk). No request body. Response:
```json
{"node": "fc-node-2", "moved": [{"sandbox": "sb-…", "to": "fc-node-3", …}], "lost": [{"sandbox": "sb-…", "reason": "…"}]}
```
Unknown node: 404.

## POST /fleet/balance

Admin only. Request `{"max?": 1, "gap?": 0.25}`. When the load difference between the busiest and the idlest node exceeds `gap`, it moves persistent sandboxes, at most `max` at a time. Response `{"moves": [{"sandbox", "from", "to", "ok"}]}`.

```bash
curl -s -X POST $CP/fleet/balance -H "Authorization: Bearer $TOKEN" -d '{"max":2,"gap":0.2}'
```

## GET /routes

Admin only. The sandbox → node route table. Response `{"routes": {"<sandbox-id>": {Node, Class, Durable, Created, Project, Meta, DiskMiB, Priority}}}` (field names follow `lifecycle.Route`; see CONTRACT §1).

## GET /metrics

Admin only. Prometheus text (`Content-Type: text/plain; version=0.0.4`). Metric names (never changed once published):

| Metric | Labels | Meaning |
| --- | --- | --- |
| `cairn_node_up` | `node` | Whether the node is alive (1 / 0) |
| `cairn_node_vms_total` | `node` | Total number of VMs on the node |
| `cairn_node_vms_running` | `node` | Number of running VMs |
| `cairn_node_mem_mib_running` | `node` | Memory used by running sandboxes (MiB) |
| `cairn_node_mem_budget_mib` | `node` | Node memory budget (MiB) |
| `cairn_node_load` | `node` | Load (the larger of the slot-usage and memory-usage ratios) |
| `cairn_nodes_alive` | — | Number of live nodes |
| `cairn_nodes_total` | — | Total number of nodes |
| `cairnboxes` | `class, priority, node, project` | Number of sandboxes counted by these four labels |
| `cairn_network_requests_pending` | — | Number of pending egress-relaxation requests |

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/metrics
```

## GET /audit

Admin and members. The tail of this replica's audit log, **newest first**.

Query: `?n=200&project=web`. `n` defaults to 200 with a maximum of 5000 (beyond that the default is used). `project` filters. Members see only rows for their own account's projects (by the row's `project`, or `for_project` if absent); rows without a project are visible only to admin. Response:
```json
{"events": [{"op": "create", "status": 201, "project": "web", "sandbox": "sb-123", "…": "…"}]}
```
With multiple replicas, each replica has its own log. Egress-relaxation requests and decisions (`network_relax_requested` / `_approved` / `_denied`) carry `project`, so members can see these rows for their own projects (before D79 they did not, and members could not see them).

**Exec and files (D80)**: every `exec` (synchronous and within a session; for streaming ones only the request is recorded) and every file read or write leaves a row in the chain: `op: exec` (`cmd` has common secrets redacted and is truncated to 200 characters, plus `user`, `exit`, `status`, `s`, and `oom_killed` for those killed by the OOM killer), `files_write` / `files_read` (`path`, and `bytes` for writes), all carrying `project`, `sandbox`, `by`. With a deployer token, `by` is written as `deployer` (no longer `default`, which is the same as the default account's name). Redaction only recognizes shapes such as `Authorization: Bearer …`, `xxx_TOKEN=…`, `sk-…`/`ghp_…`/`cairn_…`, and **is not a guarantee**; other secrets typed by hand into a command line still end up in the log (the node's own log has them anyway). Set `CP_AUDIT_EXEC=0` on the control plane to turn these rows off.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/audit?n=50&project=web"
```

## GET /audit/verify

Admin only. The audit log is a hash chain (each row carries `prev` and `h`); if a row is altered or deleted, `ok=false` from that point on. Response:
```json
{"lines": 10234, "legacy": 120, "ok": true, "first_bad": 0, "path": "…/audit.jsonl"}
```
`legacy` is the number of unhashed rows from before the chain began. Errors: 404 `this replica keeps no audit file`; 500.

## POST /templates/everywhere · POST /layers/\<id\>/everywhere

Distribute a template / layer to all live nodes; see [images.md](images.md).

## POST /sandboxes/import

Lands a durable sandbox's bundle on a node; see [sandboxes.md](sandboxes.md). For the bundle list see `GET /store/bundles` in [state.md](state.md).

## GET /events/stream

Any authenticated caller (members too). `text/event-stream`. It first sends `event: hello`, after which each `data: {"type": "node|sandbox|ticket|batch", "at": <unix seconds>}` indicates that objects of that type have changed (the control plane polls its backends every 2 seconds and compares digests); on receiving it, the client re-fetches the corresponding list; one `: keepalive` is sent per round. `ticket` comes from the intake service and `batch` from the evaluation service; if the corresponding service is not configured, those two types do not occur.

The browser's `EventSource` cannot set headers, so `?token=` can be used (server.go supports it, but the token will end up in logs).

```bash
curl -sN -H "Authorization: Bearer $TOKEN" $CP/events/stream
```

## Other

- `GET /ui/`, `GET /ui/<file>`: the static workbench, no authentication.
- `/eval/<path>`, `/intake/<path>`: forwarded; see [README](README.md).

## GET /abuse/signals

Deployer only (D58). Once a minute the control plane looks over the running sandboxes: it takes the node-side metrics of the last 10 minutes ([/metrics](sandboxes.md)), and flags a sandbox when its **average CPU exceeds 90% of the vCPUs allocated to it** (`ABUSE_CPU_PCT`, 0 disables) or its **average egress exceeds a threshold** (`ABUSE_EGRESS_BYTES_PS`, off by default); each sandbox is flagged at most once per signal kind within 30 minutes. **Observe only, no action taken**: signals go to the audit log (`abuse_signal`), the metric `cairn_abuse_signals_total{kind}`, and this endpoint; whether to stop the sandbox is the deployer's decision (`POST /accounts/<a>/suspend`). Nothing is judged when samples are insufficient (the sandbox is too young, or the window covers less than 80%). Response: `{"enabled": true, "signals": [{sandbox, project, kind, value, limit, window_s, at}]}`, newest first, at most 200.

## GET /status

**Public** (no token; D61). Coarse-grained status for status pages: `{"status": "operational" | "degraded" | "outage", "components": {"api", "registry", "database", "storage", "sandboxes"} → "operational" | "degraded" | "outage", "updated": <unix seconds>}`. Rules: the route registry, the database, or all nodes being unavailable = `outage` (HTTP **503**, convenient for external liveness probes); other component failures or some nodes being unavailable = `degraded` (200). It contains **no** node names, hosts, or error text. The console has a page that needs no login, `/console/status/`, which shows it and refreshes every 30 seconds. `/health` is the detailed version for the deployer (including node names and errors).
