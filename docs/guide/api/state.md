# 状态:卷与 bundle(State)

本文覆盖上下文 5:卷(Volume)和 bundle 存储。检查点与原地回滚属于单台沙箱,写在 [sandboxes.md](sandboxes.md);导出 / 导入 / 迁移也在那里。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md),数据与持久化见 [docs/009](../../009-security-and-data.md)。调用约定见 [README](README.md)。

卷是一个 ext4 镜像,归 Project,住在某台节点上。一个卷一次只许一个写者;读者可以多个,但不与写者共存。

## GET /volumes

调用者。成员只见本账户的。响应:
```json
{"volumes": [{"id": "…", "project": "web", "node": "node-a", "name": "cache", "size_mib": 1024, "created": 1767225600.0, "attached": {"sb-123": "rw"}, "allocated_mib": 87}]}
```
`attached`:沙箱 id → `rw` / `ro`。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/volumes
```

## POST /volumes

调用者。控制面在最空闲的活节点上建一个 ext4 镜像并记录归属。

请求:
```json
{"name": "cache", "size_mib?": 1024, "project?": "web", "node?": "node-a"}
```
`size_mib` 默认 1024;`node` 只有 admin 有效(成员带了会被忽略)。响应 **201**:卷信息(同上一项)。错误:403/404 Project;429 存储配额等,见 CONTRACT §1。

```bash
curl -s -X POST $CP/volumes -H "Authorization: Bearer $TOKEN" -H "X-Project: web" -d '{"name":"cache","size_mib":2048}'
```

## POST /volumes/from-archive

调用者。**body 是 tar(原始字节,不是 JSON)**,文件系统直接从 tar 的树生成(`mkfs -d`,不挂载)。harness 目录、依赖缓存适合这样成卷。

查询参数:`?name=&size_mib=&node=`(`node` 仅 admin)。Project 用 `X-Project` 头或 `?project=`(body 是 tar,不能放 JSON 字段)。响应 **201**:卷信息。请求体受 `CP_MAX_BODY_MIB` 限制(413)。

```bash
tar -C ./cache -cf - . | curl -s -X POST "$CP/volumes/from-archive?name=cache&size_mib=1024" \
  -H "Authorization: Bearer $TOKEN" -H "X-Project: web" --data-binary @-
```

## GET /volumes/\<id\>

调用者(卷的 Project 所在账户)。响应:卷信息。404:不存在或不是你的。

## DELETE /volumes/\<id\>

调用者。响应由卷服务给出,见 CONTRACT §1;**卷还挂着 → 409**。

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" $CP/volumes/vol-1
```

## 创建沙箱时挂卷

`POST /sandboxes` 的 `volumes: [id | {id, mount?: "/mnt/<name>", ro?: false}]`:

| 规则 | 结果 |
| --- | --- |
| 沙箱放在卷所在的节点 | 指定了别的 `node` → 409 |
| 卷必须是本项目的 | 否则 403 |
| 几个卷在不同节点 | 404 |
| 冷启动 | 模板快照里没有卷设备,所以带卷的沙箱总是冷启动;启动后按顺序以 `/dev/vdX` 挂到 `mount` |
| 写者 | 一个卷一个写者;读者(`ro:true`)可多个但不与写者共存,节点回 429 |
| 生命周期 | 沙箱销毁即解挂;暂停 / 恢复保持挂载 |

## 带卷迁移

`POST /sandboxes/<id>/migrate`(admin,见 [sandboxes.md](sandboxes.md)):先把附着的卷导到目标节点(同 id),卷还被别的沙箱附着则 409 什么都不动;成功后源节点副本删除、记录改到新节点,回 `volumes:[id]`。节点死了的接管(evacuate)**不带卷**(卷在它盘上)。

节点侧(内部,合同 §2):`GET /volumes/<id>/export`(GNU sparse tar;有运行中的写者 409)、`POST /volumes/import`(body 为该 tar,已有 409)。

## GET /store/bundles

**仅 admin**(不在 `memberPaths`)。列出对象存储里的 durable 沙箱 bundle。响应 `{"bundles": […]}`,条目字段见 CONTRACT §1。`POST /sandboxes/import` 的 `bundle` 来自这里或 export 的结果。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/store/bundles
```

## POST /store/gc

**仅 admin**。对象存储的垃圾回收。响应是 GC 的统计对象,字段见 CONTRACT §1。

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/store/gc
```
