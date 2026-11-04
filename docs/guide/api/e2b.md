# E2B 形状的口

本文覆盖控制面的第二个端口:E2B SDK 用的 REST 形状(创建 / 列出 / 暂停 / 恢复 / 删除沙箱、读模板)。路径与 [CONTRACT §1](../../../CONTRACT.md) 不冲突所以单独一个端口;合同写在 §1a,实现是 `cp/internal/e2b/api.go`,领域背景见 [docs/002](../../002-domain.md),决策 D40。**它只是控制面的一张皮**:E2B SDK 随后要连的沙箱内数据面(envd)我们不提供,见下面的兼容矩阵。调用约定(Project、角色)见 [README](README.md);§1 主口见 [sandboxes.md](sandboxes.md)。

## 端口与鉴权

| 项 | 值 |
| --- | --- |
| 端口 | 环境变量 `CP_E2B_PORT`,**设了才启动**;约定 `8710`(容器内),宿主默认映射 `18710`,即 `http://127.0.0.1:18710` |
| 鉴权 | `X-API-Key: <成员令牌或 CP_TOKEN>`;也认 `Authorization: Bearer`。令牌与 §1 的相同(成员令牌见 [tenancy.md](tenancy.md)) |
| Project | `X-Project: <id>`,可选,默认调用者账户的 `default`;不是你的 → 403 |
| 错误体 | `{"code": 404, "message": "…"}`(与 §1 的 `{"error"}` 不同) |
| 免鉴权 | `GET /health` → `{"ok": true, "surface": "e2b"}` |
| 默认模板 | `CP_E2B_DEFAULT_TEMPLATE`:SDK 没指明模板(或写 `base`)时用它;没配就冷启动基础模板 |

从 E2B SDK 指过来:把它的 API URL 指向 `http://<host>:18710`,`E2B_API_KEY` 设成我们的令牌。实际接线的步骤与限制见兼容矩阵。

## 路由

列出的就是全部;其他路径 → `404 {"code":404,"message":"no route"}`。沙箱不存在或不是你账户的 → `404 sandbox not found`。

### POST /sandboxes

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

调用者。响应是**数组**(上面的对象),成员只见本账户的,admin 见全部。查询参数(E2B 的 metadata / state 过滤、分页)不处理。

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

### GET /templates

响应数组:`[{"templateID": "py312", "aliases": ["py312"], "buildID": "py312", "cpuCount": 2, "memoryMB": 1024, "public": false, "createdAt": "…", "node": "node-a"}]`,每台活节点的每个模板快照一项。

## 兼容矩阵

图例:**可用** = 行为与 E2B 一致;**部分** = 有但语义不同;**没有** = 不提供,用右列替代。

### 控制面(本口)

| E2B 能力 | 状态 | 说明 / 替代 |
| --- | --- | --- |
| 创建沙箱(`templateID`、`timeout`、`metadata`、`envVars`) | 部分 | 见映射表:`templateID` 必须是我们已有的模板名;`envVars` 只垫在 exec 环境下面;没有 cpu / 内存请求字段,规格跟模板走 |
| 列出沙箱(`GET /sandboxes`、`/v2/sandboxes`) | 部分 | 返回全部可见沙箱,不支持 metadata / state 过滤与分页 |
| 查询 / 删除 / 暂停 / 恢复 | 可用 | |
| `connect` | 可用 | 恢复并描述 |
| `timeout` / `refreshes` | 部分 | 只改空闲 ttl,不是 E2B 的绝对结束时间 |
| 读模板 | 部分 | 只读各节点的模板快照;`public` 恒 false;没有别名管理 |
| 构建模板(`POST /templates` 等) | 没有 | 用 §1 的 `POST /builds`(oci / commit / archive),见 [images.md](images.md) |
| 卷 | 没有 | 用 §1 的 `/volumes`,见 [state.md](state.md) |
| 沙箱指标 / 日志端点 | 没有 | 用 §1 的 `GET /sandboxes/<id>/events`、`/metering/sandboxes/<id>`,见 [sandboxes.md](sandboxes.md)、[metering.md](metering.md) |
| 团队 / API key 管理 | 没有 | 用成员与令牌,见 [tenancy.md](tenancy.md) |
| 速率与请求体限制 | 不适用 | `CP_RATE_PER_MIN` / `CP_MAX_BODY_MIB` 在 §1 的主口里实现,本口代码里没有 |

### 数据面(envd,沙箱内 49983 端口)

E2B SDK 创建沙箱后,会去连 `49983-<id>.<domain>`(Connect-RPC)做命令、文件、PTY。**我们不提供 envd**,所以 SDK 的下面这些调用会连接失败。

| E2B SDK 调用 | 状态 | 用什么 |
| --- | --- | --- |
| `commands.run` / 后台命令 | 没有 | `POST /sandboxes/<id>/exec`、`/exec/stream`(主口 18700) |
| `files.read` / `write` / `list` / `watch` | 没有 | `PUT|GET /sandboxes/<id>/files?path=`(整个文件,1 GiB 上限);目录列表用 exec `ls` |
| `pty` | 没有 | `POST /sandboxes/<id>/sessions {pty:true}` 与 `write` / `read` |
| 端口:`49983-<id>.<domain>`、`<port>-<id>.<domain>` 子域 | 没有 | `ANY /sandboxes/<id>/ports/<port>/<path>`,支持 WebSocket 升级 |
| `get_host(port)` | 没有 | 同上,URL 是控制面的路径而不是子域 |

结论:E2B SDK 可以用来**管理沙箱的生命周期和读模板**;要在沙箱里跑命令、传文件、开终端,用 Python SDK `cairnbox`(形状照 E2B 的 SDK,走主口)或直接调 [sandboxes.md](sandboxes.md) 的端点。
