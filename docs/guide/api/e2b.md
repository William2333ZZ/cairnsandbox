# E2B 形状的口

本文覆盖控制面的第二个端口:E2B SDK 用的 REST 形状(创建 / 列出 / 暂停 / 恢复 / fork / 快照 / 删除沙箱、读模板),以及 SDK 对沙箱的数据面(envd:命令、文件、终端)。路径与 [CONTRACT §1](../../../CONTRACT.md) 不冲突所以单独一个端口;合同写在 §1a,实现是 `cp/internal/e2b/api.go`,领域背景见 [docs/002](../../002-domain.md),决策 D40。**它只是控制面的一张皮**:E2B SDK 随后要连的沙箱内数据面(envd)我们不提供,见下面的兼容矩阵。调用约定(Project、角色)见 [README](README.md);§1 主口见 [sandboxes.md](sandboxes.md)。

## 端口与鉴权

| 项 | 值 |
| --- | --- |
| 端口 | 环境变量 `CP_E2B_PORT`,**设了才启动**;约定 `8710`(容器内),宿主默认映射 `18710`,即 `http://127.0.0.1:18710` |
| 鉴权 | `X-API-Key: <成员令牌或 CP_TOKEN>`;也认 `Authorization: Bearer`。令牌与 §1 的相同(成员令牌见 [tenancy.md](tenancy.md)) |
| Project | `X-Project: <id>`,可选,默认调用者账户的 `default`;不是你的 → 403 |
| 错误体 | `{"code": 404, "message": "…"}`(与 §1 的 `{"error"}` 不同) |
| 免鉴权 | `GET /health` → `{"ok": true, "surface": "e2b"}` |
| 默认模板 | `CP_E2B_DEFAULT_TEMPLATE`:SDK 没指明模板(或写 `base`)时用它;没配就冷启动基础模板 |

从 E2B SDK 指过来:`E2B_API_URL` 与 `E2B_SANDBOX_URL` 都设成 `http://<host>:18710`,`E2B_API_KEY` 设成我们的令牌(完整接线见下面的\"数据面\"一节,实测结果见兼容矩阵)。

## 路由

下面列出的就是全部(`/v2/…` 与同名的 v1 路径同义);其他路径 → `404 {"code":404,"message":"no route"}`。沙箱不存在或不是你账户的 → `404 sandbox not found`。

### POST /sandboxes · POST /v2/sandboxes

调用者。请求:
```json
{"templateID?": "py312", "template?": "py312", "timeout?": 600, "metadata?": {"k": "v"}, "envVars?": {"A": "1"}}
```
映射到 §1 的创建:

| E2B 字段 | 我们的 |
| --- | --- |
| `templateID`(或 `template`) | 我们的**模板名**,写成 `template_key`,`boot: snapshot`(热启动);`"base"` 或缺省 → 用 `CP_E2B_DEFAULT_TEMPLATE`(没配就冷启动基础模板) |
| `timeout`(秒) | `ttl_idle_s`;`≤ 0` 或缺省不设 |
| `metadata` | 记在路由上(`route meta`),节点看不到;值转成字符串 |
| `envVars` | `spec.env`:垫在这台沙箱每次 exec 的环境下面(没有 init 进程可持有它们) |
| (固定) | `class: persistent` |

响应 **201**:
```json
{"sandboxID": "sb-123", "templateID": "py312", "clientID": "node-a", "alias": "py312", "state": "running",
 "startedAt": "2026-11-03T08:00:00Z", "endAt": "2026-11-03T08:10:00Z", "cpuCount": 2, "memoryMB": 1024, "diskSizeMB": 512,
 "metadata": {"k": "v"}, "project": "web", "envdVersion": "none", "envdAccessToken": ""}
```
`state`:`running` | `paused` | `unknown`(sandbox 为 unreachable / failed)。`endAt` 由 `ttl_idle_s` 减去已空闲的秒数算出,没设 ttl 为空串。`envdVersion: "none"` 表示没有 envd。

错误:`code` 即创建失败的状态码(402 / 429 配额与容量等,同 [sandboxes.md](sandboxes.md)),`message` 是原因。

```bash
curl -s -X POST http://127.0.0.1:18710/sandboxes -H "X-API-Key: $TOKEN" -H "X-Project: web" \
  -d '{"templateID":"py312","timeout":600,"metadata":{"job":"42"},"envVars":{"MODE":"dev"}}'
```

### GET /sandboxes · GET /v2/sandboxes

调用者。响应是**数组**(上面的对象),成员只见本账户的,admin 见全部。查询参数:`state=running,paused`、`metadata=k=v&k2=v2`(整体 URL 编码)、`limit` 与 `nextToken`(下一页的令牌在响应头 `x-next-token`;令牌就是列表偏移)。

### GET /sandboxes/\<id\>

响应:上面的对象。

### DELETE /sandboxes/\<id\>

响应 **204**。失败时 `code` 是节点状态码,`message: "kill failed"`。

### POST /sandboxes/\<id\>/pause · resume

响应 **204**(对应 §1 的 pause / resume)。

### POST /sandboxes/\<id\>/connect

先恢复沙箱(已运行也无妨),再描述它:**200** 上面的对象。

### POST /sandboxes/\<id\>/timeout · refreshes

请求 `{"timeout": 300}`(秒;`≤ 0` 或缺省按 300)。响应 **204**。对应节点的 `POST /sandboxes/<id>/ttl {ttl_idle_s}`,只设空闲 ttl。

### POST /sandboxes/\<id\>/fork · /snapshots · PUT /network · GET /metrics

- `fork` `{"count?": 1, "timeout?": 秒}` → **201** `[{"sandbox": {…}}]`;克隆整台运行中的虚机,成为同 Project 的路由。
- `snapshots` `{"name?": …, "memory?": …}`(两个字段都忽略)→ **201** `{"snapshotID": "snap-…", "names": ["snap-…"]}`;快照 id 当 `templateID` 即可创建新沙箱。`GET /snapshots` 列出本 Project 的,`DELETE /templates/<snapshotID>` 删除(**204**,只能删自己的快照,admin 任意)。
- `PUT …/network` `{"allowOut?": […], "denyOut?": […]}` → **204**;owner / admin 才能用,见兼容矩阵。
- `GET …/metrics` → 数组 `[{timestamp, timestampUnix, cpuCount, cpuUsedPct, memUsed, memTotal, memCache, diskUsed, diskTotal}]`。

### GET /templates

响应数组:`[{"templateID": "py312", "aliases": ["py312"], "buildID": "py312", "cpuCount": 2, "memoryMB": 1024, "public": false, "createdAt": "…", "node": "node-a"}]`,每台活节点的每个模板快照一项。

## 数据面:envd(沙箱里的命令、文件、终端)

E2B SDK 创建沙箱后会连到沙箱里的 envd 做命令、文件、PTY。我们在**同一个端口**上服务这一半,把请求经节点转到沙箱里的 sandboxd(它在 vsock 上多了一个 envd 监听,只有宿主连得上),所以不需要泛域名、TLS 和每沙箱一个主机名。

**接线**(Python 与 JS 的官方 SDK 相同):

```bash
export E2B_API_URL=http://<host>:18710          # 控制面(上面的路由)
export E2B_SANDBOX_URL=http://<host>:18710      # 同一个地址:SDK 对沙箱的所有请求都发到这里,靠 E2b-Sandbox-Id 头区分
export E2B_API_KEY=<成员令牌或 CP_TOKEN>
```

```python
from e2b import Sandbox
sbx = Sandbox.create(timeout=300)
print(sbx.commands.run("echo hi").stdout)
```

- **协议**:Connect 的 JSON 编码(官方 SDK 默认就是 JSON,不是二进制 protobuf):一元调用 `application/json`,服务端流 `application/connect+json`。明文 `http://` 下 SDK 说的是 HTTP/1.1(即使 SDK 的 `http_version` 默认是 `"2"`),我们只实现 HTTP/1.1。
- **鉴权**:创建响应里的 `envdAccessToken` 是绑定到**这一台沙箱**的令牌(`HMAC(CP_ENVD_SECRET 或由 CP_TOKEN 派生, "envd|"+沙箱 id)`,各副本共享、不落盘),SDK 以 `X-Access-Token` 发来。别的沙箱的令牌打不开这台(401);令牌泄漏只影响一台沙箱。令牌只出现在**创建、connect、fork** 的响应里(这些要写权限),`GET /sandboxes`、`/v2/sandboxes`、`/sandboxes/<id>` 里的 `envdAccessToken` 恒为空,所以只读密钥列得出沙箱、拿不到能执行命令的令牌。沙箱不存在或已被杀 → **502** 且消息含 `was not found`(SDK 的 `is_running()`、`SandboxNotRunningException` 就靠它判断)。账户被暂停时,写类请求被拒,免得中继把暂停的沙箱恢复。
- **审计**:控制面对每个数据面请求写审计(`op: envd`),`Start` 带命令行(`cmd`)、`cwd`、`user`、`pty`、`tag`,文件请求带路径;节点另写一条 `op: envd`。这些都在 `GET /audit`。
- **用户**:SDK 的 `user=` 经 `Authorization: Basic` 带到沙箱;不指定时用 `agent`(不是 root,也不是 E2B 的 `user`;请求里写 `user` 而沙箱没有这个账号时,当作 `agent`)。`root` 需要显式写。
- **环境变量**:创建时的 `envVars` 对 envd 起的进程同样可见(节点在创建与恢复时写进沙箱的 `/run/sandboxd/env.json`)。
- **要求**:沙箱的基础镜像里是带 envd 的 sandboxd(`recipes/base-debian-13` 重新构建;已有模板的快照里是旧 sandboxd,要重建模板)。旧镜像的沙箱上数据面请求得到 503,消息说明沙箱没有 envd。
- **`envdVersion`**:报 `0.5.7`。SDK 按它决定行为(`e2b/envd/versions.py`):0.5.7 是我们完整实现的最高版本(支持 octet-stream 上传、stdin、默认用户;不含文件元数据与监视条目)。

**进程跨暂停与回滚还在**:进程活在虚机内存里,所以暂停、检查点、回滚之后 `commands.list()` 里还有它,`commands.connect(pid)` 能接回同一个进程继续收输出(E2B 没有这条;一致性用例 `cairn` 组覆盖)。

## 兼容矩阵(官方 SDK 实测)

**测的是什么**:官方 `e2b` Python SDK 2.63.0(31 条)与 npm `e2b` 2.63.0(20 条)对着同一个控制面,脚本是 [ops/e2b-conformance.py](../../../ops/e2b-conformance.py) 与 [ops/e2b-conformance.mjs](../../../ops/e2b-conformance.mjs),结果文件在 [docs/research/e2b-conformance/](../../research/e2b-conformance/)。2026-11-05 在一台 Linux 机器上(Firecracker、jailer 开、1 个节点)跑,**全部通过**。**这不代表别的 SDK 版本、别的语言(Go 等)或别的用例也通过**;SDK 升级可能改变请求形状,升级后要重跑。

图例:**可用** = 一致性用例通过;**已实现未测** = 代码有、没有用例;**部分** = 有但语义不同;**没有** = 不提供。

### 控制面

| E2B 能力 | 状态 | 说明 |
| --- | --- | --- |
| 创建(`POST /sandboxes`、`/v2/sandboxes`):`templateID`、`timeout`、`metadata`、`envVars` | 可用 | `timeout` 是**空闲** ttl,不是 E2B 的绝对结束时间;`templateID` 是我们的模板名或快照 id,`base` 或缺省走 `CP_E2B_DEFAULT_TEMPLATE`(没配就冷启动基础模板) |
| 创建:`allow_internet_access`、`network.allowOut/denyOut` | 已实现未测创建时的这几个字段 | `true` → 出口 `audited`(全放行并记录);`false` 或只有 `denyOut` → `closed`;有 `allowOut` → `allowlist`(IP/CIDR 进 `allow_direct`)。不带时用 Project 的默认策略(**不是** E2B 的"默认放开互联网") |
| 创建:`autoPause`、`volumeMounts`、`mcp`、`secure`、`iam` | 没有 | 忽略这些字段(空闲超时本来就会暂停持久沙箱) |
| 列出(`GET /sandboxes`、`/v2/sandboxes`):`state`、`metadata` 过滤、`limit` 与 `nextToken` 分页 | 可用(过滤);分页已实现未测 | 分页令牌是列表偏移 |
| 查询、删除、`pause`、`connect`(v1 与 v2,`timeout` 延长寿命)、`timeout`/`refreshes` | 可用 | `pause` 后 `Sandbox.connect` 恢复,文件还在 |
| `fork`(`POST /sandboxes/<id>/fork`,`count`) | 可用 | 克隆整台运行中的虚机(内存与磁盘),克隆成为同 Project 的路由。这是我们的强项 |
| 快照:`create_snapshot`、`list_snapshots`、`delete_snapshot`,用快照 id 当模板创建 | 可用 | 对应"提交成模板"(D37)。快照名是 `snap-<项目标记>-<随机>`,不接受自选名字(`names` 里就是这个 id) |
| `update_network` | 可用 | **只有 owner 与 admin**:成员放宽策略要审批,这个口没有审批队列,成员得用原生 `PUT /sandboxes/<id>/network`(403 里说明)。E2B 的"整体替换"语义用 `rules: []` 清掉旧规则实现;E2B 的 `rules`(按主机改请求头)不支持 |
| `get_metrics` | 可用 | 内存、CPU 来自节点的 cgroup 采样;`diskUsed` 恒 0 |
| 读模板(`GET /templates`) | 部分 | 只读各节点的模板快照;`public` 恒 false |
| 模板构建(`Template.build()`、`/v3/templates`、tags)、团队与密钥管理、沙箱日志(`/logs`)、`/sandboxes/metrics`(批量) | 没有 | 构建用原生 `POST /builds`([images.md](images.md));成员与令牌见 [tenancy.md](tenancy.md);日志见原生 `/events` |

### 数据面(envd)

| E2B SDK 调用 | 状态 | 说明 |
| --- | --- | --- |
| `commands.run`:stdout / stderr 分开、退出码、非零退出抛异常、`envs`、`cwd`、`user`、命令不存在 = 127 | 可用 | 输出**边产生边到达**(用例用 `sleep` 验证了时序,不是结束后一次性给) |
| `commands.run(background=True)`、`list`、`kill`、`connect` 重新接入 | 可用 | |
| `send_stdin`、`close_stdin`(`stdin=True`) | 可用 | |
| 命令超时(`timeout=`)只断连接,进程与沙箱继续跑 | 可用 | 与 E2B 一致:客户端超时不杀进程 |
| `pty.create`、`send_stdin`、`resize`、`kill`、读输出 | 可用 | |
| `files.read` / `write`(文本、字节、20 MiB)、`write_files`、缺失文件抛 NotFound | 可用 | 上传支持 multipart 与 octet-stream;**不支持** `gzip=True`(415);multipart 整体缓冲在沙箱内存里,上限 128 MiB,大文件请用 SDK 默认的流式 octet-stream |
| `files.list`(`depth`)、`exists`、`get_info`、`make_dir`、`rename`、`remove` | 可用 | |
| `files.watch_dir` | 可用 | 靠每 250 ms 轮询目录快照,没有 inotify:重命名显示为"删除 + 创建",大目录树上有开销 |
| `commands` 的 `StreamInput`(客户端流)、`/files/compose`、`/init`、`/metrics`、`/freeze` 等 envd 内部接口 | 没有 | `StreamInput` 要 HTTP/2 全双工,SDK 的 `send_stdin` 用的是一元的 `SendInput`,不受影响 |
| 端口:`<port>-<id>.<domain>` 子域、`get_host(port)` | 没有 | 用原生 `ANY /sandboxes/<id>/ports/<port>/<path>`(支持 WebSocket 升级) |

### 没覆盖到的

- 没有 Go、Java 等其他语言的 E2B SDK 的测试。
- 一致性用例都在**单节点**上跑;多节点下 envd 经控制面找到所属节点的路径与单节点相同,但没有单独测。
- 没有压力和并发测试;没有对 `commands.run` 的延迟做过基准。
- 暂停中的沙箱收到 envd 请求会被节点透明恢复(与原生接口一致),但没有用例单独覆盖"暂停时直接 `commands.run`"。
