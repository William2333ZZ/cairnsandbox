# E2B 兼容设计(2026-11-05)

状态:**设计稿,未动手。** 用户要求"尽量兼容 E2B"。D40/D43 当时只做了控制面 REST,并写下"若接入方只会讲 envd 再做 envd 壳"的可推翻条件;现在用户明确要兼容,条件满足。依据:读了 E2B 仓库里的 `spec/openapi.yml`、envd 的 `envd.yaml`、`process.proto`、`filesystem.proto`,和 Python SDK 的 `connection_config.py`、`sandbox_sync/main.py`、`sandbox/main.py`(都是 2026-11-05 读的 main 分支);以及我们现有的 `cp/internal/e2b/api.go` 与 [api/e2b.md](../guide/api/e2b.md)。

**没验证的**:我没有把官方 SDK 跑起来对着我们打过,下面所有"能映射"都是读协议和读我们接口得出的,不是实测。SDK 内部怎么发 Connect 请求(proto 二进制还是 JSON、是否强制 HTTP/2)我没读到,见 §7。

## 1. E2B 的接口面有三块

| 块 | 谁实现 | 协议 | 我们现状 |
|---|---|---|---|
| 控制面(API) | `api.<domain>` | REST,`X-API-Key` | 8710 端口,约 8 条路由(D40) |
| envd(沙箱内的数据面) | 每个沙箱里一个守护进程,端口 49983 | REST(`/files`、`/health`…)+ Connect-RPC(`process.Process`、`filesystem.Filesystem`) | **没有**,所以官方 SDK 的 `commands.run`、`files.*` 用不了 |
| 沙箱端口转发 | `<port>-<sandboxID>.<domain>` | 泛域名 HTTP | 我们有端口暴露(D39),形状不同 |

## 2. 关键发现:不需要泛域名

SDK 读环境变量 `E2B_SANDBOX_URL`(或构造参数 `sandbox_url`);设了它,SDK 访问沙箱的所有请求都发到这一个 URL,不再拼 `<port>-<id>.<domain>`。并且每个请求带头 `E2b-Sandbox-Id` 和 `E2b-Sandbox-Port`(49983),访问令牌走 `X-Access-Token`。所以**一个入口按头路由**就够:`E2B_API_URL` 和 `E2B_SANDBOX_URL` 都指向我们 8710,控制面把 envd 请求按 `E2b-Sandbox-Id` 转给对应节点。不用泛域名、TLS、每沙箱一个主机名。D40 当时说 envd "要求泛域名入口"是没读到这个覆盖项,这条结论要更正。

(也有 `E2B_DEBUG=true`:SDK 走 `http://localhost:<port>`,只适合本机试。)

## 3. 控制面:对照 E2B 现行路由

E2B 现在的路由(节选,`spec/openapi.yml`)与我们 8710 的差距:

| E2B | 我们 | 做法 |
|---|---|---|
| `POST /sandboxes`、`POST /v2/sandboxes`(新字段:`autoPause`、`allow_internet_access`、`network`、`volumeMounts`、`secure`…) | 只有 v1 的 `/sandboxes` | 加 v2;`allow_internet_access`/`network` → 我们的出口策略;`autoPause` → 空闲自动暂停(我们本来就有 ttl 暂停);`volumeMounts` → 我们的卷 |
| `GET /v2/sandboxes`(过滤、分页) | 只有无过滤的列表 | 加 `state`、`metadata` 过滤与分页 |
| `POST /v2/sandboxes/{id}/connect` | 有 v1 connect | 加 v2 |
| `POST /sandboxes/{id}/fork` | 我们有 fork,没映射 | 映射到 §1 的 fork;**这是我们的强项,对得上** |
| `POST /sandboxes/{id}/snapshots`、`GET /snapshots` | 我们有检查点和"提交成模板" | 映射到"提交成模板"(D37):快照 ID 可当 `templateID` 再创建。**语义是否一致要对着 SDK 测**,见 §7 |
| `PUT /sandboxes/{id}/network` | 有 `PUT /sandboxes/<id>/network` | 形状翻译 |
| `GET /sandboxes/{id}/metrics`、`/sandboxes/metrics` | 有 `/metrics` | 形状翻译 |
| `GET /v2/sandboxes/{id}/logs` | 无对应 | 暂不做 |
| `/v3/templates` 等模板构建 API、tags、teams、unstable/* | 无对应 | 暂不做;模板构建 SDK 的 `Template.build()` 会不可用,写进兼容矩阵 |

## 4. envd:要实现的东西

**REST**(`envd.yaml`):`GET /health`(204)、`GET /files`(下载)、`POST /files`(上传,multipart 或原始字节)、`POST /files/compose`;`/init`、`/freeze`、`/collapse`、`/metrics`、`/envs` 是 E2B 内部或边缘的,先不做(SDK 是否会调 `/envs`、`/metrics` 要测)。`/files` 的签名参数(`signature`、`signature_expiration`)只在 SDK 有访问令牌时用,我们可以不签名而直接用 `X-API-Key` 鉴权(要对 SDK 实测)。

**Connect-RPC `process.Process`**:
| RPC | 映射 |
|---|---|
| `Start`(服务端流:start / data / end / keepalive 事件) | 我们的 `exec/stream`(节点已经返回 ndjson 事件)。E2B 的 `commands.run` 发 `/bin/bash -l -c <cmd>`;`stdin: true` 时保持标准输入打开 |
| `Connect`(接入已有进程) | 控制面记一张 pid/tag → 会话表;**我们现在的 exec 没有"后台进程 + 重新接入"** |
| `List`、`SendSignal`(SIGTERM/SIGKILL)、`CloseStdin`、`SendInput`、`Update`(改 PTY 大小) | 需要沙箱里能按 pid 操作进程;`SendInput` 对应我们的会话 write |
| `StreamInput`(客户端流) | 需要 HTTP/2 全双工;先不做,`SendInput` 够用 |

**Connect-RPC `filesystem.Filesystem`**:`Stat`、`MakeDir`、`Move`、`ListDir`、`Remove`、`WatchDir`/`CreateWatcher`…。我们的文件接口只有按路径读写整个文件,**没有** list/stat/mkdir/move/remove/watch。两条路:①经 exec 在沙箱里跑 `stat`/`ls`/`mkdir`/`mv`/`rm`(快,但慢且依赖镜像里有这些命令、要处理引号);②在 sandboxd(Rust)里加这些操作。推荐②做 `Stat/ListDir/MakeDir/Move/Remove`,watch 先不做。

## 5. 架构

```
E2B SDK ──REST──▶ cp:8710 (控制面,已有)
        └─Connect/REST + 头 E2b-Sandbox-Id──▶ cp:8710 (新:envd 面) ──▶ 节点 ──vsock──▶ sandboxd
```
- 放在 cp 的 8710 端口里,和控制面路由并存:路径不冲突(`/process.Process/*`、`/filesystem.Filesystem/*`、`/files`),唯一撞名的 `/health` 靠 `E2b-Sandbox-Id` 头区分。
- 鉴权:SDK 访问沙箱不一定带 `X-API-Key`(它用 `X-Access-Token`,值是创建响应里的 `envdAccessToken`)。设计:创建响应里的 `envdAccessToken` 填一个**绑定到该沙箱的短令牌**(由 cp 签发并校验),envd 面只认它(并校验沙箱存在、属于该令牌的账户)。这样令牌泄漏只影响一台沙箱。
- 数据面仍走我们现有的转发,**不在沙箱里装 envd 守护进程**。这点和 E2B 不同,也是我们的出口审计、预算计量保持有效的原因:所有命令、文件都经过控制面和审计。
- `envdVersion` 要报一个版本号,SDK 会按它决定行为(例如 `files` 的默认用户,>=某版本才有)。**要选一个 SDK 接受且我们能完整实现的版本**,需要读 `e2b/envd/versions` 常量(没读到)。

## 6. Protobuf 与 Connect 的实现方式(需要你定)

控制面是 Go,目前没有 protobuf 依赖。选项:
- **A. `connectrpc.com/connect` + `google.golang.org/protobuf`,生成代码入库。** 最稳:Connect 的帧格式、错误模型、流式、JSON/二进制两种编码都由库处理。代价:新增两个依赖(都是成熟的),并要把 E2B 的 `.proto` 生成物放进仓库,许可证见 §8。
- **B. 只用 `google.golang.org/protobuf`(动态消息),自己写 Connect 帧。** 少一个依赖,Connect 的流式帧和错误结尾要自己写,容易与官方 SDK 在边角上不一致。
- **C. 手写最小 protobuf 编解码,零依赖。** 与本仓库"依赖少"的风格一致,消息只有十几种;但边角(未知字段、packed、optional)出错的代价最高。

**推荐 A。** 兼容性是这件事的全部价值,不该在编解码上省。

## 7. 没读到、动手前必须先确认的

1. Python/JS SDK 发 Connect 请求时用二进制还是 JSON、是否强制 HTTP/2(影响 A/B/C 选择和 h2c 要不要开)。
2. `envdVersion` 的取值与 SDK 各版本的门槛。
3. `commands.run` 的完整往返:SDK 是否在 `Start` 之后立刻调别的 RPC(例如 `/envs`、`Connect`)。
4. `files.write/read/list` 走 REST 还是 RPC,各用哪个。
5. 快照 `POST /sandboxes/{id}/snapshots` 与我们"提交成模板"的语义是否一致(E2B 快照是持久的、可被 `templateID` 引用)。
6. 官方 JS SDK 与 Python SDK 是否行为一致。

办法:**先把官方 SDK 装进临时虚拟环境,对着一个只打印请求的假服务跑一遍 `Sandbox.create / commands.run / files.write / files.list`**,把 §7 逐条读成事实,再写代码。

## 8. 许可证

E2B 的 `spec/` 与 SDK 是 Apache-2.0,envd 的 `.proto` 也在其中;我们是 Apache-2.0,可以带生成物,但要在 `NOTICE` 写明来源与版本。**我没有核对 E2B 仓库根的 LICENSE 文本**,动手前要看一眼。

## 9. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| P0 取证 | 装官方 SDK 到临时环境,对着假服务抓请求,把 §7 填成事实,写进本文 | §7 六条各有结论 |
| P1 envd 最小集 | `/health`、`Process.Start`(流)、`SendInput`、`CloseStdin`、`SendSignal`、`/files` 读写;`envdAccessToken` | 官方 Python SDK:`Sandbox.create()` 后 `commands.run("echo hi")` 与 `files.write/read` 通过 |
| P2 文件系统 | sandboxd 加 `Stat/ListDir/MakeDir/Move/Remove`;对应 RPC | `files.list/exists/make_dir/rename/remove` 通过 |
| P3 控制面补齐 | v2 创建/列表/connect、fork、snapshots、network、metrics | SDK 的 `create/connect/pause/kill/set_timeout/fork/get_info/get_metrics` 通过 |
| P4 进程与 PTY | 进程表、`List/Connect/Update`、PTY | `commands.list/connect`、`pty.create/send_stdin/resize` 通过 |
| P5 一致性测试进 CI | 官方 Python 与 JS SDK 的一组用例对着 compose 栈跑(需要 KVM,放自托管 runner) | 用例集 + 通过率写进兼容矩阵([api/e2b.md](../guide/api/e2b.md)) |

每期结束都更新兼容矩阵,**写通过了什么、没通过什么**,不写"兼容 E2B"这种笼统说法。

## 10. 不做

模板构建 API(`Template.build()`)与 tags、teams/计费 API、sandbox 日志 API、泛域名与 TLS 入口、envd 内部接口(`/init`、`/freeze`、`/collapse`、`/fsfreeze`)、`StreamInput`(客户端流)、`WatchDir`。其中模板构建与泛域名若有用户需要再评估。

## 11. 需要用户定的事

1. §6 的 protobuf 方案(推荐 A)。
2. 允许我在临时虚拟环境里安装官方 `e2b`(PyPI)和 `e2b`(npm)SDK 做 P0 取证与一致性测试(下载第三方包)。
3. 优先级:先 P0–P1(`commands.run` 与文件读写通了就能接大多数 agent 框架),再往后做;还是你想要别的顺序。
4. 首批对接对象:Python SDK、JS SDK 都要,还是先 Python。
