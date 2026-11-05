# HTTP API 参考

本目录是 CairnSandbox 沙箱云控制面(fc-cp)的 HTTP 接口参考,按 [docs/002](../../002-domain.md) 的八个上下文分文件,每个端点一节:谁能调、请求、响应、错误、一个 `curl`。字段与状态码以 [CONTRACT.md](../../../CONTRACT.md) §1 / §1a 和 `cp/internal/httpapi/server.go` 为准;合同没写清的细节本文写"见 CONTRACT §1",不猜。节点层接口(CONTRACT §2)是内部接口,控制面与节点同版本部署,这里不展开。

## 怎么调

| 项 | 说明 |
| --- | --- |
| Base URL | 控制面默认 `http://127.0.0.1:18700`(容器内 `http://fc-cp:8700`)。下文 `curl` 用 `$CP` 表示它,`$TOKEN` 表示你的令牌 |
| 鉴权 | `Authorization: Bearer $TOKEN`。除 `/health`、`/ui`、`/login` 外都要。服务端也认 `?token=` 查询参数(见 server.go),但令牌会进日志和历史,不要用 |
| 未设 CP_TOKEN | 合同:未设置时不校验。部署都应设置 |
| Project | 创建沙箱、卷、构建等归属 Project 的请求,用 `X-Project: <id>` 头、body 的 `project` 字段或 `?project=<id>` 指定;三者都没有就是调用者账户的 `default`。别的账户的 Project → 403,不存在 → 404 |
| 数据格式 | 请求与响应 body 都是 JSON(`Content-Type: application/json`)。例外:文件的 `PUT`/`GET`(原始字节)、`/builds/archive` 与 `/volumes/from-archive` 的请求体(tar)、`exec/stream`(逐行 JSON)、`/metrics`(Prometheus 文本)、`/events/stream`(SSE)、账单 `?format=csv` |
| 错误 | `{"error": "…"}`,配合 HTTP 状态码。个别错误多带字段(如配额 429 带 `used_mib`)。错误文字不承诺稳定,按状态码和 `error` 前缀判断。E2B 口(第二个端口)的错误体是 `{code, message}`,见 [e2b.md](e2b.md) |

### 状态码

| 码 | 含义 |
| --- | --- |
| 200 | 成功 |
| 201 | 已创建(沙箱、卷、账户、成员、构建、检查点、结算单、inbox 条目) |
| 202 | 已受理但未生效:成员放宽沙箱网络策略,等 owner 批准([sandboxes.md](sandboxes.md)) |
| 400 | 请求坏了:JSON 不合法、字段校验失败(网络规则、priority、结算时段重叠等) |
| 401 | 令牌无效、已过期或被轮换掉 |
| 402 | 当日花费到 Project 配额上限(`quota: daily budget`);模型代理超预算也用 402(见合同 §4) |
| 403 | 角色不够(`deployer only` / `owner only`)或不是你的账户 / Project |
| 404 | 不存在,或存在但属于别的账户(对你不可见) |
| 409 | 冲突:卷还挂着、检查点重名、网络请求已决定、卷与节点不一致等 |
| 413 | 请求体超过 `CP_MAX_BODY_MIB` |
| 429 | 无节点有容量;配额(沙箱数、存储)到上限;非 admin 令牌超速率(带 `Retry-After`) |
| 502 | 节点或后端服务不可达;暴露端口上没人监听 |
| 503 | 节点 down 或 draining;`/eval`、`/intake` 没配置后端 |

## 角色

| 角色 | 是谁 | 能做什么 |
| --- | --- | --- |
| deployer / admin | 持有 `secrets/cp.token`(即 `CP_TOKEN`)的部署者 | 一切;是所有账户的 admin。节点、迁移、价目表、模板 / 层分发、凭证保险库、`/metrics` 等只有它能调 |
| account owner | 某账户里 `role: owner` 的成员 | 在本账户内:建 Project、增删成员、改配额与项目默认网络、批准网络放宽、结算账单 |
| member | 某账户里 `role: member` 的成员 | 本账户的沙箱、模板、卷、构建、账单(读)、审计(读);收紧网络立即生效,放宽要审批 |

成员可访问的路径前缀(`memberPaths`):`/intake`、`/health`、`/me`、`/eval/versions`、`/eval/evolve`、`/sandboxes`、`/templates`、`/accounts`、`/projects`、`/metering`、`/volumes`、`/builds`、`/layers`、`/network/requests`、`/audit`,另有 `/events/stream`。其余路径对成员一律 `403 {"error":"deployer only"}`。在这些前缀之内,个别端点仍要 owner 或 admin,每节标明。

## 限制

| 限制 | 值 | 超了 |
| --- | --- | --- |
| 请求体大小 | `CP_MAX_BODY_MIB`,默认 512 MiB(镜像归档与卷 tar 走请求体) | 413 |
| 速率 | `CP_RATE_PER_MIN`,默认 600 次 / 分钟 / 非 admin 令牌(令牌桶);设 0 关闭;admin 不限 | 429 `{"error":"rate limit","per_min":…}` 带 `Retry-After: 1` |
| 单个文件 | 1 GiB(`/sandboxes/<id>/files`) | 见 [sandboxes.md](sandboxes.md) |

## 目录

| 文件 | 上下文([docs/002](../../002-domain.md)) | 内容 |
| --- | --- | --- |
| [tenancy.md](tenancy.md) | 1 租户与访问 | 账户、Project、成员与令牌、配额、项目默认网络、`/me`、`/login` |
| [sandboxes.md](sandboxes.md) | 2 沙箱生命周期、8 执行面 | 创建 / 查询 / 暂停 / ttl / exec / 会话 / 文件 / fork / 检查点 / 网络 / 端口 / 迁移 / 导出 |
| [state.md](state.md) | 5 状态 | 卷、bundle 存储;检查点见 sandboxes.md |
| [images.md](images.md) | 4 镜像与模板 | 模板、层、构建(oci / commit / archive)、分发 |
| [network.md](network.md) | 6 网络与出口 | 凭证保险库、放宽审批、规则语法 |
| [metering.md](metering.md) | 7 计量与账单 | 价目表、账单、结算、用量 |
| [webhooks.md](webhooks.md) | 1 租户与访问 | 项目事件推送:注册、签名、重试、事件表 |
| [ops.md](ops.md) | 3 节点与容量 | 节点、fleet、routes、`/metrics`、`/audit`、事件流(admin) |
| [e2b.md](e2b.md) | E2B 形状的口 | 第二个端口、路由映射、兼容矩阵 |

其他:`GET /health`(免鉴权)→ `{ok, replica, registry:{kind, ok, where}, nodes:{name:{alive, running, total, mem_free_mib, last_err}}, routes}`;`/eval/<path>` 与 `/intake/<path>` 把请求转发到评测服务与接单服务(去掉前缀,带控制面的 bearer 和 `X-User`),它们的接口见 CONTRACT §3、§8,不在本参考内。版本与兼容承诺见 [../compatibility.md](../compatibility.md)。

```bash
export CP=http://127.0.0.1:18700
export TOKEN=$(cat secrets/cp.token)
curl -s $CP/health
curl -s -H "Authorization: Bearer $TOKEN" $CP/me
```
