# Images and templates (Images)

This page covers context 4: templates (Template, a template snapshot: memory + writable layer, used for warm starts), layers (Layer, a read-only ext4 layer stacked on top of the base image), builds (Build, which turns an OCI image / sandbox state / docker save archive into a layer or template), and cross-node distribution. For the contract see [CONTRACT §1](../../../../CONTRACT.md); for the domain design see [docs/002](../../../002-domain.md) (Chinese). For how to use them when creating a sandbox, see [sandboxes.md](sandboxes.md). For calling conventions see [README](README.md).

## Concepts

| Term | What it is |
| --- | --- |
| Layer `layers` | A read-only ext4 layer with an id like `L-xxxxxxxx`; pass `layers:[id]` when creating a sandbox to stack it on top of the base image; produced by `pack` or by a `kind:oci` / `archive` build |
| Template | A warm-start snapshot on a node; it has a name (`key`); `template_key` points to it |
| `template_key` | When creating a sandbox this can be a **template name** (the requested configuration must match the template, otherwise 400), or a 16-digit hexadecimal key (used only as a placement hint) |
| everywhere | Distribute a layer / template to all live nodes (admin) |

## GET /templates

Caller. Template snapshots on each live node. Response:
```json
{"templates": [{"key": "py312", "name?": "py312", "node": "node-a", "template": "ubuntu-24.04", "vcpu": 2, "mem": 1024, "layers": ["L-1a2b3c4d"], "from?": "…", "created": 1767225600.0, "mem_mib": 1024, "upper_mib": 40}]}
```
See CONTRACT §1 for more fields.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/templates
```

## DELETE /templates/\<name\>

An admin can delete any template; a member can only delete templates committed by their own project (`kind: commit`, completed). It removes the template, and its package in the store, from all live nodes, after which the name can be reused; sandboxes already started from it are unaffected. Response `{"deleted": "<name>", "nodes": ["node-a"]}`. Errors: 403 `not your template`; 404 no such template. (D75)

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" $CP/templates/my-tpl
```

## GET /layers

Caller. Response `{"layers": [{id, name, node, bytes, from, oci?}]}`.

## POST /builds

Caller. Asynchronous build; returns **201** immediately:
```json
{"id": "…", "project": "web", "kind": "oci", "input": {…}, "node": "node-a", "status": "running", "created": 1767225600.0}
```
`status`: `running` → `done` | `failed`. Poll with `GET /builds/<id>`.

### kind: oci

Pulls the image root filesystem into a read-only layer on the least busy live node.

Request:
```json
{"kind": "oci", "image": "python:3.12-slim", "name?": "py312", "project?": "web", "node?": "node-a"}
```
`image` can be `ghcr.io/org/app:tag`. `node` only takes effect for admin (it is dropped if a member sends it).

### kind: commit

On the node where the sandbox lives, commits its current state (memory + writable layer) as a template named `name`.

Request:
```json
{"kind": "commit", "sandbox": "sb-123", "name": "my-tpl", "note?": "after setup", "project?": "web"}
```
If the sandbox does not exist or is not yours → 404 `no sandbox …`.

Errors: 403/404 for Project; for the rest see CONTRACT §1 (`code` is decided by the build layer).

```bash
curl -s -X POST $CP/builds -H "Authorization: Bearer $TOKEN" -H "X-Project: web" \
  -d '{"kind":"oci","image":"python:3.12-slim","name":"py312"}'
curl -s -X POST $CP/builds -H "Authorization: Bearer $TOKEN" \
  -d '{"kind":"commit","sandbox":"sb-123","name":"my-tpl"}'
```

## POST /builds/archive

Caller (the `node` parameter only takes effect for admin). The way to bring an image in when the node cannot reach the image registry: **the body is a `docker save` or OCI layout tar (raw bytes)**; the control plane only forwards it and does not store it.

Query parameters: `?name=&node=`. Specify the Project with the `X-Project` header or `?project=`. Response **201** Build (same as above). The request body is limited by `CP_MAX_BODY_MIB` (413).

```bash
docker save python:3.12-slim | curl -s -X POST "$CP/builds/archive?name=py312" \
  -H "Authorization: Bearer $TOKEN" --data-binary @-
```

## GET /builds

Caller. Members see only their own account's builds. Response `{"builds": […]}`.

## GET /builds/\<id\>

Caller. Response: the Build, with `result` (`done`) or `error` (`failed`) and `finished` added once it completes. 404: does not exist or is not yours.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/builds/bld-1
```

## POST /layers/\<id\>/everywhere

Admin only (403 for non-admin). Exports the layer from a live node that has it (GNU sparse tar: ext4 + descriptor) and imports it to every node that lacks it, keeping the id unchanged.

Response:
```json
{"layer": "L-1a2b3c4d", "nodes": {"node-a": "present", "node-b": "imported", "node-c": "error…"}, "bytes": 123456, "s": 4.2}
```

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/layers/L-1a2b3c4d/everywhere
```

## POST /templates/everywhere

**Admin only** (server.go checks this explicitly; non-admin → 403 `deployer only`). Distributes a template to all live nodes.

Request: `{"spec": {…}}`; see CONTRACT §1 for the shape of `spec`. See CONTRACT §1 for the response.

```bash
curl -s -X POST $CP/templates/everywhere -H "Authorization: Bearer $TOKEN" -d '{"spec": <see CONTRACT §1>}'
```

## GET /builds/\<id\>/log

Build log (D64, currently only for `kind: oci`). While pulling the image, the node records progress lines: resolving the image, layer count, each layer downloaded / applied, writing ext4, completion or the reason for failure; each line carries the number of seconds since the build started. Response `{"build": "…", "lines": ["[   0.4s] resolving python:3.12-slim on …", …], "next": 7}`; `?since=<n>` returns only the lines after line n (pass the previous `next` back to poll). The log is **in the node's memory**, disappears one hour after the build ends, and is lost when the node restarts; it returns 404 / 503 when the build ran on another node or the node has not been upgraded yet. Whoever can see the build can see its log. In the console's "Templates and images" page, each oci build has a "Build log" button, which refreshes every 2 seconds while the build is in progress.
