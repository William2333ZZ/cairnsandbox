# 从 E2B 迁移

我们不是 E2B 的完整替身,而是两层并存([决定 D40、D43](../../decisions.md)):

- **控制面那一半 E2B 的 SDK 能直接用**:第二个端口(默认 **18710**)说 E2B 的 REST,`X-API-Key` 就是你的令牌。建、列、查、暂停、恢复、杀、改超时、连接、列模板都行。
- **数据面那一半没有**:E2B 的 SDK 把命令、文件、PTY 发给沙箱里的 `envd`(`49983-<id>.<domain>`),我们没有 `envd`。这部分换成我们的 Python SDK,走 18700。

迁移的做法因此是:**管沙箱生命周期的代码可以原样不动,执行与文件的代码换成我们的 SDK。**

## 逐项对照

| E2B 里 | 能否原样用 | 在我们这里 |
| --- | --- | --- |
| `Sandbox.create(template, timeout, metadata, envs)` | 能,指向 18710 | `POST /sandboxes {templateID, timeout, metadata, envVars}`。`templateID` 是我们的**模板名**(热启动)或 `base`;`timeout`(秒)对应空闲 TTL(`ttl_idle_s`);`metadata` 记在路由上,节点看不到 |
| `Sandbox.list()` | 能 | `GET /sandboxes` 或 `/v2/sandboxes`,成员只见本账户的 |
| 取一台 / `Sandbox.connect(id)` | 能 | `GET /sandboxes/<id>`、`POST /sandboxes/<id>/connect`(恢复并返回描述) |
| `pause()` / 恢复 | 能 | `POST /sandboxes/<id>/pause`、`/resume`,204 |
| `kill()` | 能 | `DELETE /sandboxes/<id>`,204 |
| `set_timeout()` / refresh | 能 | `POST /sandboxes/<id>/timeout` 或 `/refreshes {timeout}`,204 |
| 列模板 | 能 | `GET /templates` → `[{templateID, aliases, buildID, cpuCount, memoryMB, createdAt, node}]`,是各节点上的模板快照 |
| `commands.run(...)` | **不能** | `sb.commands.run(cmd, cwd=, env=, timeout=, user=)`,同步返回 `CommandResult(exit, stdout, stderr)`;流式用 `sb.commands.stream(...)` |
| `files.read` / `files.write` | **不能** | `sb.files.read` / `read_text` / `write`;另有 `list`、`exists`、`remove`、`mkdir` |
| `pty` | **不能** | `sb.pty.open()` 得到 `Session`,`write` / `read` 字节流 |
| `get_host(port)`(泛域名 `<port>-<id>.<domain>`) | **不能** | `sb.ports.url(port)` 给 `http://<控制面>/sandboxes/<id>/ports/<port>/…`,要带 `Authorization: Bearer`;`sb.ports.get(port)` 替你带;WebSocket 用 `sb.ports.websocket(port)` |
| 模板构建(`e2b template build` / Dockerfile) | **不能** | `POST /builds`:OCI 镜像成层、运行中的沙箱提交成模板,见[自定义模板教程](custom-template.md);我们不读 Dockerfile |
| Volumes | **不能** | `POST /volumes`,创建沙箱时 `volumes=[…]` 附上,见[手册第 6 节](../manual.md) |
| 异步客户端、`commands` 的后台进程与 `send_stdin`、`files.watch_dir` 等 | 无 | 本 SDK 没有这些;后台进程用 `commands.run("nohup … &")` 之类自己起 |

E2B 响应里的 `sandboxID` 与我们的沙箱 id 是同一个,两个端口看到的是同一批沙箱,所以**可以混用**:用 E2B SDK 建,用我们的 SDK 执行。

`envVars` 创建时给出,落到这台沙箱的 `env`,会垫在每次我们的 `commands.run` 的环境下面(请求自己的 `env` 盖在上面);E2B 的 `envd` 那种"进程启动时的环境"不存在。

错误体在 18710 上是 `{code, message}`;在 18700 上是 `{error}`,我们的 SDK 把状态码映射成 `NotFound`(404)、`Forbidden`(403)、`NoCapacity`(429)、`QuotaExceeded`(402)、`NeedsApproval`(202)。

## 配置 E2B SDK 指向我们

两样东西:API 地址换成 `http://<控制面>:18710`,API key 换成你的令牌(成员令牌或部署者令牌;也认 `Authorization: Bearer`)。E2B SDK 一般通过环境变量 `E2B_API_URL` 与 `E2B_API_KEY` 配置;这是 E2B 自己的 SDK 的设置,具体名字与是否支持自定义 API 地址以你装的版本为准,仓库里没有对某个具体版本的 E2B SDK 做过端到端测试,保证的只是 [CONTRACT §1a](../../../CONTRACT.md) 里的 REST 形状。要选项目,加 `X-Project` 头(省略就是你账户的 default 项目)。

```bash
export E2B_API_URL=http://127.0.0.1:18710
export E2B_API_KEY=<你的令牌>
curl -s -H "X-API-Key: $E2B_API_KEY" $E2B_API_URL/sandboxes     # 先用 curl 验证口子通了
```

## 前后对比

**之前**(只用 E2B):

```python
from e2b import Sandbox

sbx = Sandbox.create(template="base", timeout=300, envs={"MODE": "dev"})
sbx.files.write("/home/user/app.py", "print('hi')")
r = sbx.commands.run("python3 /home/user/app.py")
print(r.stdout)
sbx.set_timeout(600)
sbx.kill()
```

**之后**(同一份任务,执行与文件换成我们的 SDK;文件路径里默认用户目录是 `/home/agent`):

```python
from cairnbox import Client

c = Client("http://127.0.0.1:18700", token="…", project="acme")     # 或 CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
sb = c.sandboxes.create(ttl_idle_s=300, env={"MODE": "dev"})        # E2B 的 timeout ≈ ttl_idle_s;envs → env
sb.files.write("/home/agent/app.py", "print('hi')")
r = sb.commands.run("python3 /home/agent/app.py")
print(r.stdout)
sb.set_timeout(ttl_idle_s=600)
sb.kill()
```

**折中**(生命周期继续用 E2B 的 SDK,只把数据面换掉):

```python
from e2b import Sandbox                  # 指向 18710
from cairnbox import Client           # 指向 18700

sbx = Sandbox.create(template="base", timeout=300)
sb = Client("http://127.0.0.1:18700", token="…").sandboxes.get(sbx.sandbox_id)   # 同一个 id
print(sb.commands.run("uname -a").stdout)
sbx.kill()
```

(E2B SDK 的属性名随版本变,上面 `sandbox_id` 以你装的版本为准;取不到就从 `GET /sandboxes` 的 `sandboxID` 读。)

## 迁过来之后多出来的

E2B 没有而这里有的,可以逐步用上:检查点与原地回滚、从检查点 fork、给沙箱设预算与写工单([预算、工单与报告](budget-and-ticket.md))、出口策略的 L7 规则与凭证注入、项目配额与账单、审批放宽。全表见 [SDK 参考的"和 E2B SDK 的对应"](../sdk.md)与[API 参考](../api/e2b.md)。

## 迁移前检查清单

1. 代码里 `commands.`、`files.`、`pty.`、`get_host` 的调用点,这些都要换。
2. 用到的模板是不是都在我们这边有(`GET /templates`);没有的用[自定义模板教程](custom-template.md)重做,模板名就是 `templateID`。
3. 依赖外网的步骤:我们的沙箱默认没有外网,创建时给 `network.allow_hosts`。
4. 路径里的 `/home/user` 换成 `/home/agent`。
5. 超时语义:`timeout` 是**空闲** TTL,不是最长存活;要最长存活用 `ttl_max_s`。
