# Running on several machines

> About the sandbox cloud only: the control plane and its nodes. The optional agent layer is not involved.

A Cairn cluster is one **control plane machine** (control plane, registry, database, chunk store, and optionally a node of its own) plus any number of **node machines** that run only the node agent and the microVMs. Adding a machine means starting a node there and telling the control plane its IP and token. There is no cluster formation step and nothing to configure on the other nodes.

> **Verified so far:** the exact files below (`docker-compose.node.yml`, `node.sh`, the registration call) were exercised on **one** host, with two separate Compose projects reaching each other only through the Docker bridge address `172.17.0.1` as the "private IP", and a second checkout whose `images/` held nothing but `upper-template.ext4`. That covers registration by IP, creating a sandbox on the remote node, live migration through the store, and evacuating a node that was killed. It does **not** cover two physical machines, real network latency, a firewall, TLS between the control plane and a node, or a reverse proxy in front of the control plane. If a step fails for you on real machines, that is a bug in this guide: please open an issue.

## What has to reach what

| From | To | Port | Why |
|---|---|---|---|
| control plane | each node | 8800 (`NODE_PORT`) | every API call, health polling, streaming exec; bearer token in a header |
| each node | chunk store (on the control plane machine) | 18900 | kernel and base image on demand, paused-sandbox bundles for migration and evacuation, template snapshots |

Nothing else: nodes never talk to each other (a migration is "export to the store, import from the store"), and clients only talk to the control plane.

**Security.** The node API is protected by a bearer token but speaks plain HTTP unless you set up TLS (`deploy/tls/gen-certs.sh`, D62; not wired into `node.sh`, untested here). **The chunk store has no authentication at all**, and it holds memory snapshots of sandboxes. Put all machines on a private network (VPC, WireGuard, a switch), bind both ports to the private address only, and firewall them to the machines in the cluster. Never publish 18900 or 8800 on a public interface.

## Control plane machine

Set the store's bind address in `deploy/.env` to this machine's **private** IP and start the stack as usual:

```bash
cd deploy
echo 'STORE_BIND=10.0.0.1' >> .env      # this machine's private IP
./make-secrets.sh && docker compose up -d
```

Allow your node machines' IPs to reach `10.0.0.1:18900` and nothing else. The control plane's own port (18700) stays on `127.0.0.1`; put a reverse proxy with TLS in front of it for clients.

## Each node machine

You need Docker, `/dev/kvm`, `/dev/vhost-vsock`, `/dev/net/tun` and `/dev/fuse` (same as any node), a checkout of this repository, the `fc-node` image, and one small file from the control plane machine. You do **not** need to build the kernel or the base image: the node reads them from the store.

```bash
git clone https://github.com/William2333ZZ/cairn && cd cairn
docker build -t fc-node:dev -f node/Dockerfile .
mkdir -p images/layers
rsync -S control-plane-host:cairn/images/layers/upper-template.ext4 images/layers/   # -S: it is a sparse 512 MiB file

cd deploy
./node.sh init fc-node-b 10.0.0.2 http://10.0.0.1:18900      # node name, THIS machine's private IP, the store URL
./node.sh up
```

`init` writes `deploy/node.env` (mode 0600) with a fresh token. Optional environment for `init`: `MEM_BUDGET_MIB` (per node), `MAX_VMS`, `NODE_PORT`, `FENCE_AFTER_S`.

## Register the node

On the control plane machine, as the admin (the token is in `node.env` on the node machine):

```bash
curl -fsS -X POST http://127.0.0.1:18700/nodes \
  -H "Authorization: Bearer $(cat deploy/secrets/cp.token)" -H 'Content-Type: application/json' \
  -d '{"name":"fc-node-b","url":"http://10.0.0.2:8800","token":"<NODE_TOKEN from node.env>"}'
```

(`node.sh register CP_URL CP_TOKEN_FILE` does the same from the node machine, but it needs the control plane reachable from there and an admin token on that machine, so for a loopback-only control plane the command above is simpler.) The registration is stored in the registry, so every control plane replica learns about it on its next poll.

Check it:

```bash
curl -s -H "Authorization: Bearer $(cat deploy/secrets/cp.token)" http://127.0.0.1:18700/nodes   # alive: true, health.max_vms, ...
```

Then place a sandbox there with `"node": "fc-node-b"` in the create request (otherwise placement picks the less loaded node), move one with `POST /sandboxes/<id>/migrate {"to":"fc-node-b"}`, and take over a dead node's durable sandboxes with `POST /nodes/fc-node-b/evacuate`.

## Running it for real

- **Templates and layers live on the node that built them.** To use one everywhere: `POST /templates/everywhere`, `POST /layers/<id>/everywhere`.
- **Failure handling.** `FENCE_AFTER_S` (node setting) makes a node stop its own sandboxes when the control plane has been silent that long; `AUTO_EVACUATE_AFTER_S` (control plane setting) evacuates a node that has been silent that long and must be larger than the nodes' `FENCE_AFTER_S` plus 30 s, so a partitioned node has stopped its sandboxes before they are started elsewhere.
- **Clocks.** Keep the machines' clocks in sync (chrony or similar); tokens, TTLs and audit timestamps assume it.
- **Rehearse it on one machine first.** `deploy/multinode.sh up N` runs N node containers on one host and injects crashes, partitions and a throttled store link; see `deploy/README.md` (in Chinese). It models the control plane's behaviour, not real network or hardware.

## Measured on one host

Migrating a paused 256 MiB sandbox from the node on the "remote" project to the local node through the store took 2.6 s in total (export 2.2 s, 60 MiB fetched, import 0.2 s), with the store one bridge hop away. Across a real network the export and fetch times scale with the link; `STORE_BW_MBPS` in `deploy/multinode.sh` lets you see roughly how.
