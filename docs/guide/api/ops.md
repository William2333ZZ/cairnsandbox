# 运维(节点、容量、观测)

本文覆盖上下文 3(节点与容量,Fleet)以及控制面的观测面:节点注册与排空、负载均衡、路由表、`/metrics`、审计日志与校验、模板 / 层分发入口、沙箱导入、变更事件流。除特别标明外**仅 admin**(部署者令牌),成员得 `403 {"error":"deployer only"}`。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md),部署见 [deploy/README](../../../deploy/README.md)。调用约定见 [README](README.md)。

## GET /health

免鉴权。响应:
```json
{"ok": true, "replica": "cp-1", "registry": {"kind": "file", "ok": true, "where": "…"},
 "nodes": {"node-a": {"alive": true, "running": 3, "total": 5, "mem_free_mib": 20480, "last_err": null}}, "routes": 12}
```
多副本时每个副本自报 `replica`;`registry` 是它们共享的注册表(`file` 或 `redis`)。

## GET /nodes

仅 admin。响应 `{"nodes": [{name, url, alive, health:{vms_total, vms_running, max_vms, mem_mib_running, mem_budget_mib, template_snapshots, pools, …}, load, …}]}`。`load` 是空位占用比与内存占用比里大的那个。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/nodes
```

## POST /nodes

仅 admin。注册一台节点,写进注册表,其他副本下一次轮询即认识。

请求:
```json
{"name": "node-b", "url": "http://10.0.0.2:8701", "token": "…", "token_file": "…", "reserve_vms?": 2, "reserve_mem_mib?": 1024}
```
`token` 与 `token_file` 二选一。`reserve_vms` / `reserve_mem_mib` 是这台节点给 interactive 工作留的余量;`0` = 全局默认(2 台 / 1 GiB),batch / background 只放到还剩这些余量的节点。响应 **201** `{"added": "node-b"}`。错误:400 `name and url required`。

```bash
curl -s -X POST $CP/nodes -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"node-b","url":"http://10.0.0.2:8701","token_file":"/run/secrets/node-b.token","reserve_vms":2,"reserve_mem_mib":1024}'
```

## POST /nodes/\<n\>/cordon · uncordon

不再放新沙箱 / 恢复。标志在注册表里,各副本一致;显式 `node:` 钉住的创建不受影响。响应 `{"node": "node-a", "cordoned": true}`。错误:404 节点未知。

## POST /nodes/\<n\>/drain

先 cordon,再把它上面每台 **persistent** 沙箱迁走(一个副本持租约做)。响应:
```json
{"node": "node-a", "cordoned": true, "moved": ["sb-1"], "left": ["sb-9"], "failed": []}
```
`left` 是 ephemeral 的沙箱(不搬),`failed` 是没搬成的。创建落在 draining 节点 → 503。

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/nodes/node-a/drain
```

## POST /nodes/\<n\>/evacuate

仅 admin。节点死了之后的接管:把它上面 durable 沙箱按最后一份发布的 bundle 导入到别的活节点(一次一个副本做,持租约);非 durable 的算丢失;**不带卷**(卷在死节点的盘上)。无请求体。响应:
```json
{"node": "fc-node-2", "moved": [{"sandbox": "sb-…", "to": "fc-node-3", …}], "lost": [{"sandbox": "sb-…", "reason": "…"}]}
```
未知节点 404。

## POST /fleet/balance

仅 admin。请求 `{"max?": 1, "gap?": 0.25}`。最忙与最闲节点的负载差超过 `gap` 时搬 persistent 沙箱,一次最多 `max` 台。响应 `{"moves": [{"sandbox", "from", "to", "ok"}]}`。

```bash
curl -s -X POST $CP/fleet/balance -H "Authorization: Bearer $TOKEN" -d '{"max":2,"gap":0.2}'
```

## GET /routes

仅 admin。沙箱 → 节点的路由表。响应 `{"routes": {"<sandbox-id>": {Node, Class, Durable, Created, Project, Meta, DiskMiB, Priority}}}`(字段名以 `lifecycle.Route` 为准,见 CONTRACT §1)。

## GET /metrics

仅 admin。Prometheus 文本(`Content-Type: text/plain; version=0.0.4`)。指标名(一经发布不改):

| 指标 | 标签 | 含义 |
| --- | --- | --- |
| `cairn_node_up` | `node` | 节点是否存活(1 / 0) |
| `cairn_node_vms_total` | `node` | 节点上的 VM 总数 |
| `cairn_node_vms_running` | `node` | 运行中的 VM 数 |
| `cairn_node_mem_mib_running` | `node` | 运行中沙箱占用内存(MiB) |
| `cairn_node_mem_budget_mib` | `node` | 节点内存预算(MiB) |
| `cairn_node_load` | `node` | 负载(空位与内存占用比里大的那个) |
| `cairn_nodes_alive` | — | 存活节点数 |
| `cairn_nodes_total` | — | 节点总数 |
| `cairnboxes` | `class, priority, node, project` | 按这四个标签计的沙箱数 |
| `cairn_network_requests_pending` | — | 待批的出口放宽请求数 |

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/metrics
```

## GET /audit

admin 与成员。本副本审计日志的尾部,**新的在前**。

查询:`?n=200&project=web`。`n` 默认 200,上限 5000(超出按默认)。`project` 过滤。成员只见本账户项目的行(按行里的 `project`,没有则 `for_project`);没有 project 的行只有 admin 见。响应:
```json
{"events": [{"op": "create", "status": 201, "project": "web", "sandbox": "sb-123", "…": "…"}]}
```
多副本时每个副本各有自己的日志。放宽出口的申请与决定(`network_relax_requested` / `_approved` / `_denied`)带 `project`,所以成员能看到自己项目的这些行(D79 之前没有,成员看不到)。

**执行与文件(D80)**:每次 `exec`(同步和会话里的;流式的只记请求)和文件读写都在链里留一行:`op: exec`(`cmd` 过一遍常见密钥的涂黑、截成 200 字符,`user`,`exit`,`status`,`s`,被 OOM 杀掉的带 `oom_killed`)、`files_write` / `files_read`(`path`,写的带 `bytes`),都带 `project`、`sandbox`、`by`。`by` 在 deployer 令牌时写 `deployer`(不再是和默认账户同名的 `default`)。涂黑只认 `Authorization: Bearer …`、`xxx_TOKEN=…`、`sk-…`/`ghp_…`/`cairn_…` 这类形状,**不是保证**,命令行里手敲的别的机密仍会进日志(节点自己的日志本来就有)。控制面设 `CP_AUDIT_EXEC=0` 关掉这些行。

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/audit?n=50&project=web"
```

## GET /audit/verify

仅 admin。审计日志是哈希链(每行带 `prev` 与 `h`);改一行或删一行,从那里起 `ok=false`。响应:
```json
{"lines": 10234, "legacy": 120, "ok": true, "first_bad": 0, "path": "…/audit.jsonl"}
```
`legacy` 是链开始前的无哈希行。错误:404 `this replica keeps no audit file`;500。

## POST /templates/everywhere · POST /layers/\<id\>/everywhere

把模板 / 层分发到所有活节点,见 [images.md](images.md)。

## POST /sandboxes/import

把 durable 沙箱的 bundle 落到一个节点,见 [sandboxes.md](sandboxes.md);bundle 列表见 [state.md](state.md) 的 `GET /store/bundles`。

## GET /events/stream

任何已认证调用者(成员也可)。`text/event-stream`。先发 `event: hello`,之后每条 `data: {"type": "node|sandbox|ticket|batch", "at": <unix 秒>}` 表示该类对象有变化(控制面每 2 秒轮询其后端并比对摘要),客户端收到后重拉对应列表;每轮一条 `: keepalive`。`ticket` 来自接单服务、`batch` 来自评测服务,没配置对应服务就没有这两类。

浏览器的 `EventSource` 不能设头,可以用 `?token=`(server.go 支持,但令牌会进日志)。

```bash
curl -sN -H "Authorization: Bearer $TOKEN" $CP/events/stream
```

## 其他

- `GET /ui/`、`GET /ui/<file>`:静态工作台,免鉴权。
- `/eval/<path>`、`/intake/<path>`:转发,见 [README](README.md)。

## GET /abuse/signals

仅部署者(D58)。控制面每分钟看一遍运行中的沙箱:取节点侧最近 10 分钟的指标([/metrics](sandboxes.md)),**平均 CPU 超过它分到的 vCPU 的 90%**(`ABUSE_CPU_PCT`,0 关闭)或**平均出站超过阈值**(`ABUSE_EGRESS_BYTES_PS`,默认关)就标记,每个沙箱每种信号 30 分钟内只标一次。**只观察,不动手**:信号进审计(`abuse_signal`)、指标 `cairn_abuse_signals_total{kind}` 与这个接口;要不要停,由部署者决定(`POST /accounts/<a>/suspend`)。样本不足(沙箱太年轻、窗口覆盖不到 80%)不判。响应:`{"enabled": true, "signals": [{sandbox, project, kind, value, limit, window_s, at}]}`,新的在前,最多 200 条。

## GET /status

**公开**(不要令牌;D61)。给状态页用的粗粒度状态:`{"status": "operational" | "degraded" | "outage", "components": {"api", "registry", "database", "storage", "sandboxes"} → "operational" | "degraded" | "outage", "updated": <unix 秒>}`。规则:路由注册表、数据库、或所有节点不可用 = `outage`(HTTP **503**,方便外部探活);其余组件异常或部分节点不可用 = `degraded`(200)。**不含**节点名、主机、错误文本。控制台有一个不用登录的页面 `/console/status/` 展示它,30 秒刷新。`/health` 是给部署者的详细版(含节点名与错误)。
