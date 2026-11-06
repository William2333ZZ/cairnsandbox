# State: Volumes and Bundles

This document covers context 5: volumes (Volume) and bundle storage. Checkpoints and in-place rollback belong to a single sandbox and are written up in [sandboxes.md](sandboxes.md); export / import / migration are also there. For the contract see [CONTRACT §1](../../../../CONTRACT.md), for the domain design see [docs/002 (Chinese)](../../../002-domain.md), and for data and persistence see [docs/009 (Chinese)](../../../009-security-and-data.md). For calling conventions see the [README](README.md).

A volume is an ext4 image that belongs to a Project and lives on one node. A volume allows only one writer at a time; there can be multiple readers, but not together with a writer.

## GET /volumes

Caller. Members see only their own account's volumes. Response:
```json
{"volumes": [{"id": "…", "project": "web", "node": "node-a", "name": "cache", "size_mib": 1024, "created": 1767225600.0, "attached": {"sb-123": "rw"}, "allocated_mib": 87}]}
```
`attached`: sandbox id → `rw` / `ro`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/volumes
```

## POST /volumes

Caller. The control plane creates an ext4 image on the most idle live node and records ownership.

Request:
```json
{"name": "cache", "size_mib?": 1024, "project?": "web", "node?": "node-a"}
```
`size_mib` defaults to 1024; `node` is effective only for admin (ignored if a member sends it). Response **201**: the volume info (same as the item above). Errors: 403/404 Project; 429 storage quota, etc., see CONTRACT §1.

```bash
curl -s -X POST $CP/volumes -H "Authorization: Bearer $TOKEN" -H "X-Project: web" -d '{"name":"cache","size_mib":2048}'
```

## POST /volumes/from-archive

Caller. **The body is a tar (raw bytes, not JSON)**, and the filesystem is generated directly from the tar's tree (`mkfs -d`, no mounting). This suits turning a harness directory or dependency cache into a volume.

Query parameters: `?name=&size_mib=&node=` (`node` admin only). Specify the Project with the `X-Project` header or `?project=` (the body is a tar, so it cannot carry JSON fields). Response **201**: the volume info. The request body is limited by `CP_MAX_BODY_MIB` (413).

```bash
tar -C ./cache -cf - . | curl -s -X POST "$CP/volumes/from-archive?name=cache&size_mib=1024" \
  -H "Authorization: Bearer $TOKEN" -H "X-Project: web" --data-binary @-
```

## GET /volumes/\<id\>

Caller (the account that owns the volume's Project). Response: the volume info. 404: does not exist or is not yours.

## DELETE /volumes/\<id\>

Caller. The response comes from the volume service, see CONTRACT §1; **volume still attached → 409**.

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" $CP/volumes/vol-1
```

## Attaching volumes when creating a sandbox

`volumes: [id | {id, mount?: "/mnt/<name>", ro?: false}]` in `POST /sandboxes`:

| Rule | Result |
| --- | --- |
| The sandbox is placed on the node where the volume lives | Specifying a different `node` → 409 |
| The volume must belong to this project | Otherwise 403 |
| Several volumes on different nodes | 404 |
| Cold start | The template snapshot has no volume devices, so a sandbox with volumes always cold-starts; after boot the volumes are mounted at `mount` as `/dev/vdX` in order |
| Writer | One writer per volume; readers (`ro:true`) can be multiple but cannot coexist with a writer, and the node replies 429 |
| Lifecycle | Destroying the sandbox detaches the volume; pause / resume keeps the mount |

## Migration with volumes

`POST /sandboxes/<id>/migrate` (admin, see [sandboxes.md](sandboxes.md)): first exports the attached volumes to the target node (same id); if a volume is still attached to another sandbox, it returns 409 and nothing is touched; on success the source node's copy is deleted, the record is moved to the new node, and `volumes:[id]` is returned. Takeover after a node dies (evacuate) **does not carry volumes** (the volumes are on its disk).

Node side (internal, contract §2): `GET /volumes/<id>/export` (GNU sparse tar; 409 if there is a running writer), `POST /volumes/import` (body is that tar; 409 if it already exists).

## GET /store/bundles

**Admin only** (not in `memberPaths`). Lists durable sandbox bundles in object storage. Response `{"bundles": […]}`; entry fields are in CONTRACT §1. The `bundle` for `POST /sandboxes/import` comes from here or from the result of export.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/store/bundles
```

## POST /store/gc

**Admin only**. Garbage collection of object storage. The response is the GC statistics object; fields are in CONTRACT §1.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/store/gc
```
