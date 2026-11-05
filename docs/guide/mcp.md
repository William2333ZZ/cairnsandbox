# MCP 服务器

源码:[mcp.py](../../sdk/python/cairnsandbox/mcp.py)。把沙箱云作为工具给任何会说 MCP 的 agent(Claude Code、Codex、Cursor 等)。stdio、JSON-RPC 2.0、按行分隔,零依赖。底层调用的是 [Python SDK](sdk.md);路由见 [api/sandboxes.md](api/sandboxes.md)。

## 启动

```
CAIRN_URL=http://cp:18700 CAIRN_TOKEN=… CAIRN_PROJECT=acme python3 -m cairnsandbox.mcp
```

`CAIRN_URL`(默认 `http://127.0.0.1:18700`)、`CAIRN_TOKEN`、`CAIRN_PROJECT` 从环境读(`Client()` 无参构造)。需要先 `pip install -e sdk/python`,或把 `sdk/python` 放进 `PYTHONPATH`。

### 接到 Claude Code

```
claude mcp add aios -- python3 -m cairnsandbox.mcp
```

`pip install -e sdk/python` 之后也有控制台命令 `cairnsandbox-mcp`,等价于 `python3 -m cairnsandbox.mcp`:

```bash
claude mcp add aios -- cairnsandbox-mcp
```

要传环境变量:`claude mcp add aios -e CAIRN_URL=http://cp:18700 -e CAIRN_TOKEN=$CAIRN_TOKEN -- python3 -m cairnsandbox.mcp`(或在启动 Claude Code 的 shell 里先 export,子进程继承)。

### Codex / Cursor / 其他

通用配置形状(文件位置与外层键名因客户端而异,核心是 command + args + env):

```json
{
  "mcpServers": {
    "cairn": {
      "command": "python3",
      "args": ["-m", "cairnsandbox.mcp"],
      "env": { "CAIRN_URL": "http://cp:18700", "CAIRN_PROJECT": "acme" }
    }
  }
}
```

令牌不要写在配置里,见下文安全提示。

## 工具(12 个)

所有工具的结果都是单个 text 内容块;下表"返回"指这段文本的内容(多数是 JSON 字符串)。`sandbox` 参数是沙箱 id。

| 工具 | 必填 | 可选 | 返回 | 典型用法 |
|---|---|---|---|---|
| `sandbox_create` | 无 | `template` str、`mem` int(MiB)、`cls` `persistent`\|`ephemeral`、`network` object(如 `{"allow_hosts":["pypi.org"]}`)、`env` object、`ttl_idle_s` int、`template_key` str、`layers` [str] | `{"id","status","node"}` | 开工第一步 |
| `sandbox_run` | `sandbox`、`cmd` | `cwd`、`timeout` int、`user` `agent`\|`root`、`env` object | `{"exit","stdout","stderr","timed_out"}` | 跑命令、测试、安装 |
| `sandbox_write_file` | `sandbox`、`path`、`text` | — | `{"bytes": n}` | 写整个文本文件(绝对路径) |
| `sandbox_read_file` | `sandbox`、`path` | — | 文件内容原文(UTF-8,不是 JSON) | 读结果 / 日志 |
| `sandbox_checkpoint` | `sandbox` | `name`、`note` | 检查点 dict(含 `name`、`chain_len`) | 冒险步骤之前 |
| `sandbox_rollback` | `sandbox`、`checkpoint` | — | `{"id","status","rollback_s"}` | 搞砸了就回去 |
| `sandbox_fork` | `sandbox` | `n` int(默认 1)、`checkpoint`、`cls`(默认 `ephemeral`) | 克隆 id 数组 `["sb-…"]` | 并行试多种方案 |
| `sandbox_pause` | `sandbox` | — | `{"status"}` | 暂停到磁盘;下条命令会透明恢复 |
| `sandbox_resume` | `sandbox` | — | `{"status"}` | 显式恢复 |
| `sandbox_kill` | `sandbox` | — | `{"deleted": id}` | 用完删除 |
| `sandbox_list` | 无 | — | `[{"id","status","node","class"}]` | 看令牌能见到的沙箱 |
| `sandbox_info` | `sandbox` | — | 沙箱完整 dict(状态、检查点、卷、网络、用量) | 排查 |

工具参数是 SDK 的子集:没有 `vcpu`、`disk`、`volumes`、`stdin`、`ttl_max_s`、`priority` 等;需要这些请用 [Python SDK](sdk.md)。

## 协议覆盖面

| 方法 | 行为 |
|---|---|
| `initialize` | 返回 `protocolVersion: "2025-06-18"`(固定,不协商)、`capabilities: {tools: {}}`、`serverInfo {name: "cairnsandbox", version: "0.1.0"}`、一段 `instructions` |
| `notifications/initialized` | 记下,不回复 |
| `ping` | `{}` |
| `tools/list` | 上表 12 个工具及其 `inputSchema` |
| `tools/call` | 见下 |

没有 resources、prompts、logging、采样,也没有流式输出或进度通知:`sandbox_run` 阻塞到命令结束再一次返回。其他带 `id` 的方法返回 JSON-RPC 错误 `-32601 method not found`;不带 `id` 的未知通知被静默忽略。非法 JSON 行被丢弃;处理中的未捕获异常回 `-32603`。

## 错误怎么回

工具内的失败不走 JSON-RPC error,而是正常结果加 `isError: true`:

- 控制面返回错误状态 → 文本 `"{status}: {message}"`,如 `404: no checkpoint`、`429: no node with capacity`、`402: quota: daily budget`、传输失败是 `0: transport: …`。
- 缺必填参数 → `ValueError: missing required argument(s): sandbox`;未知工具 → `ValueError: unknown tool …`;读到非 UTF-8 文件等其他异常 → `"{异常类名}: {信息}"`。均为 `isError: true`。
- 注意 `sandbox_run` 里命令本身非零退出**不是**错误:`isError` 为 false,看 JSON 里的 `exit`。

## 安全提示

- 令牌只放环境变量(`CAIRN_TOKEN`),不要写进 `.mcp.json` 等会提交到仓库的配置;用 shell profile、密钥管理器或客户端的本机私有配置注入。
- 令牌的权限就是 agent 的权限:给它一个限定项目(`CAIRN_PROJECT`)且有日预算的令牌,让配额与出网策略兜底。
- 出网默认由沙箱网络策略控制;`sandbox_create` 的 `network` 可以在创建时指定,但 MCP 工具没有放宽已有沙箱的入口(那需要 owner 审批,见 [api/network.md](api/network.md))。
