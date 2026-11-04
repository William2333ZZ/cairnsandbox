# 镜像与模板(Images)

本文覆盖上下文 4:模板(Template,模板快照:内存 + 可写层,热启动用)、层(Layer,只读 ext4 层,叠在基础镜像上)、构建(Build,把 OCI 镜像 / 沙箱状态 / docker save 归档变成层或模板)、以及跨节点分发。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md)。创建沙箱怎么用它们见 [sandboxes.md](sandboxes.md)。调用约定见 [README](README.md)。

## 概念

| 词 | 是什么 |
| --- | --- |
| 层 `layers` | 只读 ext4 层,id 形如 `L-xxxxxxxx`,创建沙箱时 `layers:[id]` 叠在基础镜像上;`pack` 或 `kind:oci` / `archive` 构建得到 |
| 模板 | 在某节点上的一份热启动快照;有名字(`key`);`template_key` 指向它 |
| `template_key` | 创建沙箱时可以是**模板名**(请求的配置须与模板一致,否则 400),或 16 位十六进制的键(仅作放置提示) |
| everywhere | 把层 / 模板分发到所有活节点(admin) |

## GET /templates

调用者。各活节点的模板快照。响应:
```json
{"templates": [{"key": "py312", "name?": "py312", "node": "node-a", "template": "ubuntu-24.04", "vcpu": 2, "mem": 1024, "layers": ["L-1a2b3c4d"], "from?": "…", "created": 1767225600.0, "mem_mib": 1024, "upper_mib": 40}]}
```
更多字段见 CONTRACT §1。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/templates
```

## DELETE /templates/\<名字\>

admin 可删任何模板;成员只能删自己项目提交(`kind: commit`、已完成)出来的。从所有活节点上删掉该模板及其在存储里的包,名字随即可以再用;已经从它启动的沙箱不受影响。响应 `{"deleted": "<名字>", "nodes": ["node-a"]}`。错误:403 `not your template`;404 没有这个模板。(D75)

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" $CP/templates/my-tpl
```

## GET /layers

调用者。响应 `{"layers": [{id, name, node, bytes, from, oci?}]}`。

## POST /builds

调用者。异步构建,立刻返回 **201**:
```json
{"id": "…", "project": "web", "kind": "oci", "input": {…}, "node": "node-a", "status": "running", "created": 1767225600.0}
```
`status`:`running` → `done` | `failed`。用 `GET /builds/<id>` 轮询。

### kind: oci

在最空闲的活节点上把镜像根文件系统拉成一个只读层。

请求:
```json
{"kind": "oci", "image": "python:3.12-slim", "name?": "py312", "project?": "web", "node?": "node-a"}
```
`image` 可以是 `ghcr.io/org/app:tag`。`node` 仅 admin 有效(成员带了会被丢掉)。

### kind: commit

在沙箱所在节点把它此刻的状态(内存 + 可写层)提交成名为 `name` 的模板。

请求:
```json
{"kind": "commit", "sandbox": "sb-123", "name": "my-tpl", "note?": "after setup", "project?": "web"}
```
沙箱不存在或不是你的 → 404 `no sandbox …`。

错误:403/404 Project;其余见 CONTRACT §1(`code` 由构建层决定)。

```bash
curl -s -X POST $CP/builds -H "Authorization: Bearer $TOKEN" -H "X-Project: web" \
  -d '{"kind":"oci","image":"python:3.12-slim","name":"py312"}'
curl -s -X POST $CP/builds -H "Authorization: Bearer $TOKEN" \
  -d '{"kind":"commit","sandbox":"sb-123","name":"my-tpl"}'
```

## POST /builds/archive

调用者(`node` 参数仅 admin 有效)。节点连不上镜像仓库时,把镜像带进来的路:**body 是 `docker save` 或 OCI layout 的 tar(原始字节)**,控制面只转发不存。

查询参数:`?name=&node=`。Project 用 `X-Project` 头或 `?project=`。响应 **201** Build(同上)。请求体受 `CP_MAX_BODY_MIB` 限制(413)。

```bash
docker save python:3.12-slim | curl -s -X POST "$CP/builds/archive?name=py312" \
  -H "Authorization: Bearer $TOKEN" --data-binary @-
```

## GET /builds

调用者。成员只见本账户的。响应 `{"builds": […]}`。

## GET /builds/\<id\>

调用者。响应:Build,完成后多 `result`(`done`)或 `error`(`failed`)、`finished`。404:不存在或不是你的。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/builds/bld-1
```

## POST /layers/\<id\>/everywhere

仅 admin(非 admin 403)。从一台有这层的活节点导出(GNU sparse tar:ext4 + 描述),导入到每台没有的节点,id 不变。

响应:
```json
{"layer": "L-1a2b3c4d", "nodes": {"node-a": "present", "node-b": "imported", "node-c": "error…"}, "bytes": 123456, "s": 4.2}
```

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/layers/L-1a2b3c4d/everywhere
```

## POST /templates/everywhere

**仅 admin**(server.go 显式检查,非 admin → 403 `deployer only`)。把模板分发到所有活节点。

请求:`{"spec": {…}}`,`spec` 的形状见 CONTRACT §1。响应见 CONTRACT §1。

```bash
curl -s -X POST $CP/templates/everywhere -H "Authorization: Bearer $TOKEN" -d '{"spec": <见 CONTRACT §1>}'
```

## GET /builds/\<id\>/log

构建日志(D64,目前只有 `kind: oci`)。节点在拉镜像时记录进度行:解析镜像、层数、每层下载完 / 应用完、写 ext4、完成或失败原因,每行带构建开始后的秒数。响应 `{"build": "…", "lines": ["[   0.4s] resolving python:3.12-slim on …", …], "next": 7}`;`?since=<n>` 只取第 n 行之后的(把上次的 `next` 传回来轮询)。日志在**节点内存里**,构建结束一小时后消失,节点重启即丢;构建跑在别的节点或节点还没升级时 404 / 503。能看这条构建的人才能看日志。控制台"模板与镜像"页每条 oci 构建有"构建日志"按钮,构建进行中每 2 秒刷新。

