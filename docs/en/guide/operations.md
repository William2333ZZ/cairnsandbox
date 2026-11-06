# Operations Manual: Failures, Metrics, Capacity

For people who deploy and are on call. The design rationale is in [003 (Chinese)](../../003-sandbox.md), [D31 (Chinese)](../../decisions.md) (multiple replicas and shared state) and [D34 (Chinese)](../../decisions.md) (checkpoints); getting started with deployment is in [deploy/README](../../../deploy/README.md); threats and residual risks are in [013 (Chinese)](../../013-threat-model.md).

**Each conclusion is tagged with its basis**: **[tested]** is run by automated tests (CI or unit tests); **[drilled]** was actually done once on the dev machine; **[from code]** is derived only from reading the code, with no fault-injection proof. Do not treat [from code] as a guarantee.

## 0. Where to look first

| What to look at | How | What it answers |
| --- | --- | --- |
| `GET /health` | No token needed | Replica name, whether the registry is reachable (`registry.ok`), and for each node `alive` / `last_err` / free memory |
| `GET /metrics` (admin) | Prometheus text | Whether dependencies are reachable, node load, sandbox counts by class, 5xx and rate-limit counts, metering heartbeat; the rules are in [deploy/alerts.yml](../../../deploy/alerts.yml) |
| `GET /audit?n=200` (admin sees everything, members see their own account) | JSON, newest first | Everything the control plane has done: `node_down`, `node_up`, `registry_error`, `reconcile_adopt`, `reconcile_delete_stale`, `evacuate`, `migrate`, `preempt`, `member_rotate` … |
| `GET /audit/verify` (admin) | JSON | Whether this replica's audit log hash chain is intact |
| Node audit | `docker logs fc-node` (one JSON per line) | What happened on the node: `destroy`, `recover`, `orphan_removed`, `export_error`, `drain_*`, `layer_*` |
| The console's nodes page | admin | Each node's liveness, load, cordon state |

## 1. Who stores what

| Component | Stores | Can it be rebuilt if lost? |
| --- | --- | --- |
| **Node** (fc-node) | The sandboxes themselves: memory, disk, checkpoints, volumes, layers, template snapshots; the authority for runtime state | No (this is the data itself) |
| **Registry** (Redis `fc-registry`) | Routes (sandbox → node, project, priority, metadata), node registrations, reconcile leases | **Yes**: routes are rebuilt by node reconciliation (see F5) |
| **Database** (Postgres `fc-db`) | Accounts, projects, members (token hashes), quotas, default networks, loosening requests, usage events, bills and settlements, volume and build records | **No**, rely on backups (see section 4) |
| **Chunk store** (`fc-store`) | The bundle that a durable sandbox publishes on every pause, archives of layers and template snapshots, image chunks loaded on demand | No; if lost, takeover and distribution stop working, while node-local copies are unaffected |
| **Control plane** (`fc-cp`) | Stateless; locally only `audit.jsonl` (one per replica) and (in file-registry mode) `routes.json` | Yes |
| Model proxy / evaluator / Agent service | Each has its own state directory: model usage counters, evaluation batches, dispatch records | No, back up as needed |

## 2. Failure playbook

Each section: **symptom → impact → does it recover by itself → what you need to do**.

### F1 A single sandbox is `failed`

When the VMM exits abnormally or the health watchdog thread finds the guest unreachable, the node marks it failed; in the next TTL cycle (1 second) it is destroyed and the jail, cgroup and tap are cleaned up, and the audit records `destroy` with the reason. At the control plane's next reconcile (≤ 1 minute) its route is deleted. [from code; `test-jail` verifies cleanup]

- What you do: nothing. If a durable sandbox had been paused at some point, it can be imported again from the last bundle (`POST /sandboxes/import`); state from a running sandbox that had never been paused is already lost.

### F2 Graceful node restart (upgrade)

`docker compose up -d fc-node` or `docker stop` (SIGTERM, wait 120 seconds): the node first **cordons itself** to stop accepting new work, **pauses persistent sandboxes to disk**, **destroys ephemeral ones**, then exits; once the new container is up, `recover()` restores sandboxes that have a paused snapshot on disk to paused, and they resume transparently on the first request. [CI's test-snap and test-net restart recovery]

- Ephemeral sandboxes are destroyed, so callers such as the evaluator must be able to rerun.
- With multiple nodes, first `POST /nodes/<n>/drain` (persistent sandboxes migrate away), then swap the image, then `uncordon`: zero interruption.

### F3 Node unreachable (host crash, container killed, network cut)

- **Symptom**: the control plane polls every 2 seconds; a failure is audited as `node_down` and `cairn_node_up{node} == 0`; its sandboxes show `unreachable` in the list; requests to those sandboxes return **503 "node X is down"** (durable ones carry `durable: true`); new creation avoids it. [tested: the node-unreachable section of test-cp; Go httpapi tests]
- **No automatic takeover by default**. This is deliberate: during a network partition the node may still be alive, with the same sandbox running in two places and writing the same bundle. **Optional automatic takeover (D54)** relies on "node self-fencing" for safety: the node sets `FENCE_AFTER_S=120` (if the control plane is silent for this long, the node itself pauses persistent sandboxes, with durable ones publishing a bundle, destroys the rest, and `fenced: true` appears in health), and the control plane sets `AUTO_EVACUATE_AFTER_S=180` (**must be greater than** the node's fence time plus a margin); then each outage triggers an automatic evacuate only once, audited as `auto_evacuate`. When both are 0 it is off. Boundary: when a node can reach neither the control plane nor fc-store, the fencing pause succeeds but the bundle is not published, and takeover uses an earlier one; fencing only guards against "the network is down" and not against "the control plane itself has a problem while the node is fine" (in that case the node would be fenced too, so do not set the window shorter than the control plane's restart time). [tested: node `fence_all`, control plane `AutoEvacuateOnce`; **not** drilled on a real network partition]
- **What you do**:
  1. Confirm the node is **really dead** (host powered off, container stopped, the network is not cut only toward the control plane); if unsure, wait first, or `cordon` it first.
  2. `POST /nodes/<n>/evacuate`: durable sandboxes are restored as paused on another live node from the **last published bundle** (what is lost is the state after that pause); non-durable ones count as `lost`; **volumes do not come along** (they are on the dead node's disk); only one replica does it at a time (lease). The response lists who was `moved` and who was `lost`. [tested: test-cp]
  3. When the node is repaired and comes back: `recover()` restores the sandboxes paused on its disk; the control plane's reconcile finds stale copies whose route already points elsewhere and **deletes the old copy** (audit `reconcile_delete_stale`), so a sandbox that was taken over never exists in two copies. [tested]
- Alerts: `AiosNodeDown`, `AiosSandboxesStranded`.

### TLS between control plane and nodes (D62)

The default is plaintext HTTP (acceptable on the docker network of one machine). Across machines you must enable TLS: (1) `deploy/tls/gen-certs.sh <directory> <node hostname>…` generates a CA and a certificate for each node (private keys stay only in the directory you give); (2) set `TLS_CERT` / `TLS_KEY` on the node (compose: `NODE_TLS_CERT` / `NODE_TLS_KEY`), and it speaks HTTPS on the original port; (3) set `NODE_CA_FILE=<ca.crt>` on the control plane, and register the node with an `https://…` URL (`NODES` or `POST /nodes`); (4) first add the new node and verify, then replace the old node with https. Certificates expire after a year: **a node with an expired certificate is treated as unreachable** (alert `AiosNodeDown`), so renew before expiry; the control plane must be restarted to re-read the CA (needed only when the CA is replaced, not when a node certificate is). Boundaries: public routes such as `/health` also go over TLS; the WebSocket tunnel from the control plane to the node is verified in the same way; other internal hops, such as node to fc-store and to the model proxy, do **not** have TLS enabled. [tested: node HTTPS unit tests, control plane unit tests against https nodes; **not** verified on a real cross-machine deployment]

### F4 A control plane replica dies or restarts

- The control plane is **stateless** (routes and node registrations are in Redis, tenants are in Postgres), so a restart loses nothing; during the restart, request connections are refused and the SDK reports `ApiError(0, transport …)`. [drilled: this deployment repeatedly restarted the production replica, and routes and sessions persisted]
- With multiple replicas you must **put a load balancer in front yourself** (we do not ship one); the workbench points at just one replica.
- Metering runs only on the replica holding the lease (once a minute). If all replicas are down for more than 1 hour, that gap is **not backfilled** (to avoid overcharging for an unknown period); gaps under 1 hour are backfilled by the actual interval. [from code `Tick`: `dt > 3600` is set to 0]
- After a restart, node reconcile completes within 30 seconds to 1 minute; sandboxes created during that time already had their routes written to the registry and are unaffected.

### F5 Registry (Redis)

- **Unreachable**: every request by sandbox id returns **503 "registry unavailable"** (not 404, since the route is most likely still there). If a sandbox was built on the node at creation but its route could not be written: the audit records `registry_error`, the **sandbox still exists**, and reconcile within a minute of the registry recovering will adopt it back. `registry.ok` in `/health` is false. [tested: `TestWhenTheRegistryIsUnreachable…`]
- **Data loss** (starting from an empty Redis, `registry-data/` wiped): routes are rebuilt by the **nodes**: when each node comes online and every minute afterwards, the control plane adopts back the sandboxes that exist on the node but not in the registry (audit `reconcile_adopt`). **Project, priority and E2B metadata** are read back from the labels stored on the node, so billing and quotas are not misattributed. [tested: `TestALostRegistryIsRebuilt…`] **[drilled: on 2026-11-03 on the dev machine I really deleted the Redis route of one of my own sandboxes; after the deletion, requests by id returned 404; 55 seconds later the route came back with the demo project, `batch` priority and metadata, field-for-field identical to before the deletion]**. Because adoption happens when a node comes online and every minute afterwards, requests by sandbox id may get 404 for **up to about a minute** between the loss and the rebuild (the registry is reachable at that point, it just lacks this route)
  - Exception: **sandboxes created before this labelling mechanism shipped** have no labels on the node, and after adoption land in the **default project** (billing and quota are recorded to default). Operators can reconcile afterwards in the database; a rebuild has no way to know what is wrong.
  - Node registrations: those in the `NODES` environment variable are re-registered when the control plane starts; ones added only via `POST /nodes` must be **added again**.
  - Leases restart automatically; tenant data is not lost (that is in Postgres).
- Alert: `AiosRegistryDown`.

### F6 Database (Postgres)

- **Unreachable**: creation returns **503 "tenancy store unavailable"** (not 404 "no project"); **member token verification fails → 401 "bad token"** (verification needs a database lookup; note it is 401, not 503); **the deployer token works as usual** (it is in memory); exec, files, pause, resume and listing of existing sandboxes **do not depend on the database** and work as usual. [tested: `TestWhenTheTenancyDatabaseIsDown…`] Metering stops and continues on recovery, with the same gap rule as F4.
- **Data loss**: restore from backup (section 4). **Sandboxes and routes are unaffected** (they are on nodes and in Redis). Accounts, members, usage, settlements, and volume and build records added after the backup are lost; **member tokens issued after the backup become invalid** and must be reissued; the volume records are lost but the volume files are still on the node. [from code; the restore itself is covered by the drill]
- Alerts: `AiosDatabaseDown`, `AiosMeteringStalled`.

### F7 Chunk store (fc-store)

- **Unreachable**: running sandboxes are **unaffected**. For a durable sandbox, **publishing the bundle fails at pause** (node audit `export_error`; the pause itself succeeds), so after this pause it has no up-to-date bundle for takeover, and you cannot count on evacuate to save it. For new sandboxes, base image and layer reads go through lazyfs: blocks in the local cache work as usual, **uncached blocks wait at most 120 seconds each and then the read fails**, and the guest sees a block device I/O error. Migration, takeover, template and layer distribution, and bundle import all fail. [from code; **no fault-injection test**]
- **Data loss**: if a bundle is lost, the durable sandbox can no longer be taken over (the node-local copy is still there); layers and template snapshots still have a copy locally on each node (only the cross-node copies are lost); base images loaded on demand have to be pushed again.
- Alert: `AiosStoreDown`.

**Storage backends (D52)**: `fc-store` has two backends, a local directory (the default, `store/`) and an S3-compatible bucket (`fc-s3`, Garage). The HTTP interface to nodes and the control plane is exactly the same, so switching backends requires no change to any caller.
- Enabling: `deploy/garage/bootstrap.sh` starts `fc-s3` and creates the bucket and keys (the keys are written only to `$SECRETS/s3.env`, 0600, not into git, not printed); `imgstore.py migrate` copies the existing `store/` into the bucket (idempotent, chunks first, then the manifest); then `STORE_BACKEND=s3 ./run-node.sh store`.
- **Rollback**: restarting `fc-store` without `STORE_BACKEND` returns to the local directory; after migration the local directory is left as it was until you confirm it can be deleted.
- When the backend errors, `fc-store` returns **502** (not 404), so a caller does not mistake "storage unreachable" for "no such chunk".
- What gets backed up becomes: `s3/` (Garage's metadata and data directories) + `secrets/s3.env`. **Do not copy the live `s3/meta/db.sqlite` directly**: first `docker exec fc-s3 /garage meta snapshot` to get a consistent snapshot, then copy the snapshot + all the small files under `meta/` (**`cluster_layout`, `data_layout`, `node_key`, `peer_list`**: if any are missing, the restored node shows "no role assigned" and returns 503 to every request) + the whole of `data/`. `deploy/garage/restore-drill.sh` turns this into a script and it **has been drilled**: it starts the copy in a separate container, reads 1004 chunks back through S3 and checks every hash, all consistent (Garage's files belong to root, so the script uses a container to copy and delete); a single-node Garage has no replicas, so **it is a single point of failure just like the local directory**; for replicas, add nodes and change `replication_factor` to 3 (see `deploy/garage/garage.toml`).

### F8 Model proxy

Model calls from the agent in a sandbox fail (402 / 502); **the sandbox itself is unaffected**. Metering reads the proxy's cumulative usage, and after recovery the next tick makes up the difference, losing nothing. [from code]

### F9 Disk full

| Where | Symptom |
| --- | --- |
| Node state disk | Creation, checkpoints and pause fail; the node audit reports errors |
| Control plane state disk | **When the audit cannot be written, requests do not report an error and the audit silently lacks rows** (write errors are ignored), so you must monitor this disk |
| fc-store disk | Bundle publishing fails (same as F7) |
| Postgres disk | Writes fail, creation gives 503, metering stops |

**Two things that grow by themselves**: `audit.jsonl` has no automatic rotation (one per replica, append-only); `usage_events` has no retention period. To rotate the audit: stop the replica, `mv audit.jsonl audit-<month>.jsonl`, start the replica (the new file's chain starts empty; verify the old file separately with `audit.Verify`).

### F10 Token leak

- Member token: `POST /projects/<p>/members/<u>/rotate` (owner, admin or the member themselves); the old token is invalidated **immediately**; look in the audit at `by` to see what was done with it.
- Deployer token: replace `secrets/cp.token` and **restart all services that read it** (control plane, evaluator, Agent service, the person at the nodes page); afterwards review the admin operations in `/audit`.

### F12 Abuse: suspending an account

**Detection**: the control plane ships with an observe-only detector (D58) that flags sustained full CPU load (by default an average of ≥ 90% of the allotted vCPUs over 10 minutes) and abnormal egress: the alert is `AiosAbuseSignal`, and `GET /abuse/signals` shows who. Legitimate builds also max out CPU, so it **does not act automatically**.

**Handling**: on finding an account abusing the platform (mining, scanning, egress flooding, violating the terms): `POST /accounts/<a>/suspend {"reason": "…"}` ([API](api/tenancy.md)). It first records the suspension, then **pauses** all of that account's running sandboxes (state kept for investigation); from then on the account's members can only view the list and single-sandbox info and delete sandboxes; reading files / events / ports is also not allowed (it would make the node resume the sandbox). For forensics: filter the audit by `project`, look at egress decisions with `GET /sandboxes/<id>/network/decisions`, and use the deployer token to enter the sandbox and inspect the scene (the deployer is not subject to suspension). When done, `POST /accounts/<a>/unsuspend`, and the owner resumes the paused sandboxes themselves. With multiple replicas: the suspension state is stored in Postgres, and all replicas recognize it within 3 seconds at most.

## 3. Metrics and alerts

**Dashboard**: `deploy/grafana/cairn-overview.json` is a ready-made Grafana overview (nodes online, running sandboxes, dependency reachability, suspended accounts and abuse signals, node memory and load, sandbox states, sandboxes on unreachable nodes, responses and 5xx, metering stalled, loosenings pending approval); choose your Prometheus data source when importing; `tests/unit/test_alerts.py` guarantees it queries only metrics that `/metrics` really has.

`GET /metrics` (admin). The rules are in [deploy/alerts.yml](../../../deploy/alerts.yml), and a unit test guarantees that every metric referenced there really exists.

| Metric | Labels | Meaning | Corresponding alert (threshold) |
| --- | --- | --- | --- |
| `cairn_node_up` | node | 1 = the most recent poll succeeded | AiosNodeDown (0 for 1 minute, critical) |
| `cairn_node_vms_total` / `_running` | node | Sandboxes on the node / running | — |
| `cairn_envd_requests_total` | kind (start, connect, process_rpc, filesystem_rpc, files, health), status (2xx/4xx/5xx) | E2B data-plane request count (D88) | — |
| `cairn_envd_active_relays` / `cairn_envd_bytes_total{dir}` / `cairn_envd_auth_denied_total` | dir | In-flight relays (a Start stream holds one for the duration of the command) / relayed bytes / number of rejected tokens | — |
| `cairn_node_mem_mib_running` / `cairn_node_mem_budget_mib` | node | Used / budgeted memory | AiosNodeMemoryHigh (> 90% for 10 minutes) |
| `cairn_node_load` | node | The larger of slot usage and memory usage (0–1) | AiosNodeLoadHigh (> 0.85 for 10 minutes); AiosNoRoomAnywhere (all nodes > 0.85, critical) |
| `cairn_nodes_alive` / `cairn_nodes_total` | — | Live nodes / registered | — |
| `cairnboxes` | class, priority, node, project | Sandboxes in the routing table | — |
| `cairnboxes_on_down_nodes` | — | Sandboxes whose route points to an unreachable node | AiosSandboxesStranded (> 0 for 2 minutes, critical) |
| `cairn_dependency_up` | dep = registry / database / store | 1 = just probed as reachable | AiosRegistryDown, AiosDatabaseDown (critical); AiosStoreDown (warning) (each for 1–2 minutes) |
| `cairn_metering_last_tick_age_seconds` | — | Seconds since the last accounting | AiosMeteringStalled (> 300 for 5 minutes) |
| `cairn_http_responses_total` | class = 2xx / 3xx / 4xx / 5xx | Response count since this replica started | AiosServerErrors (5xx share > 5% and > 0.1/s, for 10 minutes) |
| `cairn_http_rate_limited_total` | — | Number of times per-token rate limiting (429) applied | AiosRateLimiting (info) |
| `cairn_network_requests_pending` | — | Egress loosenings waiting for owner approval | AiosEgressRequestsWaiting (info, one day) |

For things with no metric that need cron: the **audit chain**: `curl -H "Authorization: Bearer $T" $CP/audit/verify`, and alert if `ok` is not true; **whether backups were done on time**: check the backup file's timestamp.

## 4. Backup and restore

**What to back up**: `db-data/` (Postgres, with `pg_dump`), `store/` (fc-store's directory), `secrets/`. **What not to back up**: Redis (F5 rebuilds it), the control plane's state directory (except the audit; back it up if you have a compliance requirement), and the sandboxes on nodes (they are the data itself; the way to back them up is durable + bundle).

```bash
docker compose exec -T fc-db pg_dump -U cp cp | gzip > backup/cp-$(date +%F).sql.gz
tar czf backup/store-$(date +%F).tgz store/
```

**Restore drill** (do not wait for an incident to do it for the first time):

```bash
gunzip -c backup/cp-<date>.sql.gz > /tmp/restore.sql
docker run -d --name fc-db-drill --memory 256m -e POSTGRES_USER=cp -e POSTGRES_DB=cp -e POSTGRES_PASSWORD=drill postgres:16-alpine
until docker exec fc-db-drill pg_isready -U cp; do sleep 1; done
docker exec -i fc-db-drill psql -q -U cp cp < /tmp/restore.sql
# Compare row counts table by table:accounts projects members quotas usage_events meter_statements volumes builds network_requests project_network prices
docker rm -f fc-db-drill
```

**Done once on the dev machine on 2026-11-03** (production database, read-only `pg_dump`, a temporary 256 MiB container, deleted after use):

| | Result |
| --- | --- |
| Backup | `pg_dump` 79 KB, 1 second |
| Restore into an empty database | 1 second, 0 errors |
| Row counts per table | accounts 2 / projects 3 / members 2 / quotas 1 / usage_events 534 / network_requests 1 / project_network 1 …… 11 tables in all, source and restore **all identical** |

This is the result on a **small database**; once the database grows, measure again (`usage_events` has no retention period, see section 5). Restoring `store/` (local directory backend) has not been drilled; the Garage restore has been drilled (see the section above).

## 5. Capacity planning

**Per node** (a starting point, not a guarantee):

| Item | How to determine it |
| --- | --- |
| `MEM_BUDGET_MIB` | Admission checks the **sum of configured memory** of all sandboxes against the budget. The memory the host leaves for this node × 0.8. Measured idle sandboxes (128 MiB configured) use 60–72 MiB, and sandboxes under real load come closer to the configured value; to be conservative, count "configured value + 40 MiB" |
| `MAX_VMS` | Limited by taps, cgroups and file handles; 32 by default on shared machines. The upper limit on dedicated machines has not been measured (see [benchmark (Chinese)](../../research/benchmark-2026-11.md)) |
| Headroom reserved for people | `reserve_vms` / `reserve_mem_mib` (default 2 sandboxes / 1 GiB): batch and background may use only what is outside the reserve, while interactive may use it all |
| Node state disk | Each sandbox's writable layer is a sparse file (`disk` in the spec defaults to 512 MiB); for actual usage see `disk_used_mib`; add layers, template snapshots, and checkpoints (each checkpoint = a copy of the writable layer + one layer of memory) |

**Cluster**:

- **N+1**: if the largest node dies, the free slots on the remaining nodes must be able to hold the persistent sandboxes that were on it; otherwise part of evacuate will fail.
- Add nodes when load stays above 0.85; `AiosNoRoomAnywhere` is the last line of defense.
- **fc-store**: each durable sandbox publishes one bundle on every pause (memory diff + writable layer, with shared layers deduplicated by content). Starting point: storage ≥ 2 × Σ(durable sandboxes' memory + used disk).
- **Postgres**: `usage_events` gets about 3–4 rows per minute per running sandbox (vcpu, memory, disk, plus egress and model tokens when present), with no retention period. 100 sandboxes running continuously ≈ 500,000 rows per day, tens of MB; check the table size once a quarter, and archive it yourself when needed (for periods already settled in `meter_statements`, older detail can be archived).
- **Audit**: one per replica; growth follows operation volume, with no rotation (F9).

## 5b. Rebuilding the base image (side by side, without touching existing sandboxes)

The base image's (`recipes/base-debian-13`) fingerprint is in the key of every template snapshot, so **replacing it in place invalidates all template snapshots and paused sandboxes**. So always build a new one side by side and then switch. D76 did this once; the steps:

1. Compile the runtime: `recipes/sbx-runtime/build.sh images/bin-out-v2` (rust container, tens of seconds).
2. Build the base image: `SANDBOXD_BIN=$PWD/images/bin-out-v2/sandboxd recipes/base-debian-13/build.sh images/base-v2`, and copy the result to `images/base/base-debian-13-v2.ext4` (**the file name carries the version**, because the store fetches by file name).
3. Push it into the store: `python3 -c "import sys; sys.path.insert(0,'node'); import imgstore; imgstore.push_remote('http://127.0.0.1:18900','base-debian-13-v2.ext4','images/base/base-debian-13-v2.ext4')"`.
4. Register the template: add `recipes/templates/debian-13-v2.json` (same kernel as the old one, with `base` pointing to the new file), and restart the node (`LAZY_STORE=http://fc-store:8900 ./run-node.sh`, which gracefully pauses and then restores running sandboxes). At this point `debian-13` (old) and `debian-13-v2` coexist, and existing sandboxes and snapshots are unaffected.
5. Verify: create a sandbox with `template: "debian-13-v2"` and check `getent hosts localhost`; in a sandbox with `mem: 1024`, write 800 MB and then `pack`; redo the development template with `BASE_TEMPLATE=debian-13-v2 python3 ops/dogfood-devloop.py prepare …` and run the Go tests.
6. **Switch** (needs a decision; the only thing that cannot be hot-rolled-back is "redoing templates"): set `DEFAULT_TEMPLATE=debian-13-v2` on the node; change the `EVAL_TEMPLATE` / `INTAKE_TEMPLATE` used by the evaluator and the Agent service likewise; redo the templates you want to keep on the new base. Delete the old `debian-13` and the like only once no sandbox uses them.

Rollback: change `DEFAULT_TEMPLATE` back to `debian-13` and restart the node; the snapshots on the old base are still there.

## 6. On-call quick reference

| Symptom | Check first |
| --- | --- |
| Request 503 "node X is down" | F3; the nodes page, `cairn_node_up` |
| Request 503 "registry unavailable" | F5; `registry.ok` in `/health`, the Redis container |
| Creation 503 "tenancy store unavailable" / all members get 401 | F6; the Postgres container |
| Creation 429 "no node with capacity" | Section 5; `cairn_node_load`, quota (429 "quota: …" is a project quota, not capacity) |
| Creation 402 "quota: daily budget" | The project's spend for the day hit the limit; Console → Projects and members, change the quota |
| Many `unreachable` in the sandbox list | F3 |
| Billing suddenly stops moving | `cairn_metering_last_tick_age_seconds`; F6 |
| A member says "my token suddenly stopped working" | Whether it expired (`expiry` / the ttl of `rotate`), whether it is rate-limited, F6 |
