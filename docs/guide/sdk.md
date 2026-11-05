# Python SDK 参考

源码:[client.py](../../sdk/python/cairnsandbox/client.py)(Client 与顶层集合)、[sandbox.py](../../sdk/python/cairnsandbox/sandbox.py)(单个沙箱)。HTTP 路由见 [api/README.md](api/README.md);括号里是该方法转发的路由,按主题查 [sandboxes](api/sandboxes.md)、[state](api/state.md)、[images](api/images.md)、[network](api/network.md)、[metering](api/metering.md)、[tenancy](api/tenancy.md)、[ops](api/ops.md)。

## 安装与配置

```
pip install -e sdk/python      # 零依赖(只用标准库),Python >= 3.9
```

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `CAIRN_URL` | 控制面地址 | `http://127.0.0.1:18700` |
| `CAIRN_TOKEN` | Bearer 令牌 | 空(不发 Authorization) |
| `CAIRN_PROJECT` | 项目,以 `X-Project` 头发送 | 空(用令牌的默认项目) |

构造参数优先于环境变量。返回值一律是服务端的 JSON(dict / list),不包装成模型;`Sandbox` 与 `CommandResult` 例外。

```python
from cairnsandbox import Client
c = Client()                                   # 读环境变量
c = Client("http://cp:18700", token="…", project="acme", timeout=600)
```

## 错误类

| 类 | 状态码 | 含义 |
|---|---|---|
| `ApiError` | 其他 4xx/5xx;传输失败时 `status=0` | 基类。属性 `.status .message .body .method .path` |
| `NotFound` | 404 | 沙箱 / 卷 / 检查点等不存在 |
| `Forbidden` | 403 | 令牌无权 |
| `NoCapacity` | 429 | 无节点有余量、卷被占用、沙箱数配额 |
| `QuotaExceeded` | 402 | 项目当日预算用尽 |
| `NeedsApproval` | 202 | 放宽出网等待项目 owner 批准;`.request` 是待批请求 |

`message` 取响应 JSON 的 `error` 字段。`NeedsApproval` 只在非 `raw`、非 `stream` 的请求里由 202 触发。`CommandResult.check()` 抛的是 `RuntimeError`,不是 `ApiError`。

## Client

`Client(url=None, token=None, project=None, timeout=600)`

子对象:`sandboxes volumes templates layers builds billing projects network`。

| 方法 | 说明 |
|---|---|
| `request(method, path, body=None, raw=False, headers=None, timeout=None, stream=False)` | 底层。`body` 为 dict(JSON)或 bytes(octet-stream);`raw=True` 返回 bytes;`stream=True` 返回未读的响应对象。`get/post/put/delete(path, ...)` 是它的简写 |
| `me()` | `GET /me` → `{user, account, projects…}` |
| `health()` | `GET /health` |
| `nodes()` | `GET /nodes` → 节点 list(含 `name`、`alive`) |

### Client.sandboxes

| 方法 | 返回 | 说明 |
|---|---|---|
| `create(template=None, mem=None, vcpu=None, disk=None, cls=None, network=None, env=None, volumes=None, layers=None, template_key=None, ttl_idle_s=None, ttl_max_s=None, retain_paused_s=None, priority=None, qos=None, boot=None, durable=None, node=None, metadata=None, **extra)` | `Sandbox` | `POST /sandboxes`。值为 `None` 的参数不发,交给服务端默认。`cls` 发为 `class`:`persistent`/`ephemeral`;`priority`:`interactive`/`batch`/`background`;`volumes`:id 列表或 `{id, mount, ro}`;`network`:`{mode, allow_hosts, allow_direct, rules, limits}`;`**extra` 原样透传 |
| `get(sid)` | `Sandbox` | `GET /sandboxes/{id}`;404 → `NotFound` |
| `list()` | `[Sandbox]` | `GET /sandboxes` |
| `import_bundle(bundle, node=None)` | `Sandbox` | `POST /sandboxes/import`。节点丢失且无人疏散时,按最后发布的 bundle 重新落地一个 durable 沙箱 |

### Client.volumes

| 方法 | 说明 |
|---|---|
| `create(name, size_mib=1024, project=None, node=None)` | `POST /volumes` → 卷 dict |
| `from_archive(name, tar_bytes, size_mib=None, node=None)` | `POST /volumes/from-archive?name=…`,请求体是 tar 字节;不挂载直接建出内容卷 |
| `list()` | `GET /volumes` → list |
| `get(vid)` / `delete(vid)` | `GET` / `DELETE /volumes/{id}` |

### Client.templates / layers / builds

| 方法 | 说明 |
|---|---|
| `templates.list()` | `GET /templates` → list |
| `templates.everywhere(spec)` | `POST /templates/everywhere`,把模板铺到所有节点;`spec` 为 dict |
| `layers.list()` | `GET /layers` → list |
| `layers.everywhere(lid)` | `POST /layers/{id}/everywhere` |
| `builds.from_oci(image, name=None, node=None)` | `POST /builds {kind:"oci"}`,OCI 镜像 → 层 |
| `builds.from_archive(tar_bytes, name, node=None)` | `POST /builds/archive?name=…`,tar → 层 |
| `builds.commit(sandbox_id, name, note=None)` | `POST /builds {kind:"commit"}`,运行中的沙箱 → 命名模板 |
| `builds.get(bid)` / `builds.list()` | `GET /builds/{id}`、`GET /builds` |
| `builds.wait(bid, timeout=1800, every=2.0)` | 轮询到 `status` 为 `done`/`failed`;超时也返回最后一次的 dict(不抛错,需自查 `status`) |

### Client.billing

`project` 缺省取 `client.project`,再缺省 `"default"`。

| 方法 | 返回 | 路由 |
|---|---|---|
| `today(project=None)` | float,当日花费 USD | `GET /metering/projects/{p}/today` → `spent_usd` |
| `statement(project=None, since=None, until=None)` | dict | `GET …/statement?from=&to=` |
| `statement_csv(project=None, since=None, until=None)` | str(CSV) | `GET …/statement?format=csv` |
| `settle(since, until, project=None)` | dict | `POST …/statements {from,to}`,出账并落库 |
| `statements(project=None)` | list | `GET …/statements` |
| `prices()` | dict | `GET /metering/prices` |
| `sandbox_usage(sid)` | list | `GET /metering/sandboxes/{id}` → `usage` |

### Client.projects

| 方法 | 说明 |
|---|---|
| `list(account=None)` | `GET /accounts/{account}/projects`;account 缺省取 `me()["account"]`,再缺省 `default` |
| `quota(project=None)` | `GET /projects/{p}/quota` |
| `set_quota(project=None, **q)` | `PUT /projects/{p}/quota`,`q` 为配额字段 |
| `members(project=None)` | `GET …/members` → list |
| `add_member(user, role="member", name=None, project=None)` | `POST …/members` |
| `default_network(project=None)` | `GET …/network` → 项目默认出网策略 |
| `set_default_network(network, project=None)` | `PUT …/network` |

### Client.network

等 owner 处理的放宽请求(`/network/requests`)。

| 方法 | 说明 |
|---|---|
| `requests(all=False)` | `GET /network/requests`(`all=True` 带 `?all=1`)→ list |
| `approve(rid)` / `deny(rid)` | `POST /network/requests/{id}/approve|deny` → 更新后的请求 dict |

## Sandbox

由 `Client.sandboxes.*` 返回。`.info` 是最近一次服务端应答(dict),`.id .status .node` 是它的只读属性;`refresh()` 重取。支持 `with`,退出时 `kill()`,吞掉所有异常。

| 方法 | 说明 |
|---|---|
| `refresh()` | `GET /sandboxes/{id}` → 新 `info`(dict) |
| `pause()` / `resume()` | `POST …/pause|resume`,更新 `info`,返回 self |
| `kill()`(别名 `delete`) | `DELETE /sandboxes/{id}` → `{deleted: id}` |
| `set_timeout(ttl_idle_s=None, ttl_max_s=None, retain_paused_s=None)` | `POST …/ttl`,返回 self。`retain_paused_s=0` = 删除前一直保留 |
| `fork(n=1, checkpoint=None, cls="ephemeral", ttl_idle_s=None, ttl_max_s=None, qos=None)` | `POST …/fork` → `[Sandbox]`(写时复制;父沙箱继续跑)。不给 `checkpoint` 即从此刻分叉 |
| `pack(name)` | `POST …/pack {name}` → dict |
| `commit(name, note=None)` | 即 `client.builds.commit(self.id, …)`,返回 build dict,用 `builds.wait` 等完成 |
| `migrate(to=None)` | `POST …/migrate`,`to` 为目标节点,缺省由调度选 |
| `diff(cwd="/work/repo", base="main")` | `GET …/diff?cwd=&base=` → dict |
| `usage()` | `client.billing.sandbox_usage(id)` |
| `budget(max_usd=None, max_tokens=None)` | `PUT …/budget` → `budget` dict |
| `ticket(ticket)` | `PUT …/ticket {ticket}` → `ticket` |
| `inbox(text, kind="note", actor=None, thread=None)` | `POST …/inbox` → dict;给沙箱里的 harness 投递消息 |
| `events(since=0, max=None)` | `GET …/events?since=&max=` → `(events, last_seq)` |
| `wait_for_report(timeout=3600, every=2.0, since=0, include_progress=False)` | 轮询 `events`,返回第一个 `type=="report"` 且 `data.state` 为 ready / waiting / blocked / failed 的事件(`{seq, ts, type, data}`);`include_progress=True` 时 progress 也返回;超时返回 `None` |

### sb.commands

| 方法 | 说明 |
|---|---|
| `run(cmd, cwd=None, env=None, timeout=None, user=None, stdin=None, landlock=None)` | `POST …/exec`(`bash -c`)。`user`:`agent`/`root`;`landlock`:`True`(默认限制)或 `{ro, rw, connect_ports}`。返回 `CommandResult`。HTTP 超时 = `(timeout or 600) + 30` |
| `stream(cmd, cwd=None, env=None, timeout=None, user=None)` | `POST …/exec/stream`(NDJSON)。生成器,产出 `("stdout"\|"stderr", text)`,最后一项 `("done", CommandResult)`。不支持 `stdin`、`landlock` |

`CommandResult`:`.exit`(int)`.stdout .stderr .timed_out`(bool,取自 `timeout` 字段)`.elapsed_s .raw`(原始 dict)`.check()`(`exit != 0` 抛 `RuntimeError`,返回 self)。命令超时不抛错,看 `timed_out`。

### sb.files

| 方法 | 说明 |
|---|---|
| `write(path, data)` | `PUT …/files?path=`;`str` 按 UTF-8,返回字节数 |
| `read(path)` | `GET …/files?path=` → bytes;不存在 `NotFound` |
| `read_text(path, encoding="utf-8")` | str |
| `list(path="/home/agent")` | 目录内名字 list,子目录带尾 `/`。实现是在沙箱里跑 `ls -1Ap`,不是专门路由 |
| `exists(path)` | bool(`test -e`) |
| `remove(path)` | `rm -rf --`,失败抛 `RuntimeError` |
| `mkdir(path)` | `mkdir -p --` |

后四个经 `commands.run` 实现,会经 `exec`(计入命令,沙箱暂停时会被唤醒)。

### sb.pty

`open(user=None, pty=True)` → `Session`(`POST …/sessions`)。`pty=False` 是无 TTY 的长驻 shell。

`Session`:`.id .pty`;`exec(cmd, timeout=60)` → `CommandResult`(`POST …/sessions/{sid}/exec`,状态跨调用保留);`write(data)`(str/bytes,原始字节流);`read(wait=0.5)` → bytes(最多等 `wait` 秒);`close()`(`DELETE …/sessions/{sid}`)。支持 `with`。

### sb.ports

| 方法 | 说明 |
|---|---|
| `url(port, path="/")` | `{client.url}/sandboxes/{id}/ports/{port}{path}`,纯字符串拼接,不发请求 |
| `get(port, path="/", headers=None)` | `(status, headers, body)`;自动加 Bearer;HTTP 错误状态不抛错而是返回 |
| `websocket(port, path="/", headers=None)` | 已完成握手(101)的原始 socket,帧自己编;非 101 抛 `RuntimeError`。带 `X-Project`(客户端设了项目时),握手 key 每次随机 |

### sb.checkpoints

| 方法 | 说明 |
|---|---|
| `create(name=None, note=None)` | `POST …/checkpoints` → `{name, chain_len, paused_s…}` |
| `list()` | `GET …/checkpoints` → list |
| `rollback(name)` | `POST …/rollback {checkpoint}`,原地回滚,更新 `sb.info` 并返回;无此点 `NotFound` |
| `drop(name)` | `DELETE …/checkpoints/{name}` |

### sb.network

| 方法 | 说明 |
|---|---|
| `get()` | `refresh()` 后取 `info["network"]` |
| `set(**policy)` | `PUT …/network`,字段 `mode allow_hosts allow_direct rules limits`,返回 `network` dict。收紧立即生效;成员放宽 → 抛 `NeedsApproval` |
| `decisions()` | `GET …/egress` → `decisions` list(出网判决记录) |

## 和 E2B SDK 的对应

**已经在用 E2B 官方 SDK 的,不用换:** 把 `E2B_API_URL` 与 `E2B_SANDBOX_URL` 指到控制面的 E2B 口(18710)就能用,覆盖面与实测结果见 [api/e2b.md](api/e2b.md)。下面是**我们自己的 SDK** `cairnsandbox` 与 E2B SDK 在调用形状上的对应(走主口 18700,多出检查点、回滚、预算等)。

| E2B | 本 SDK |
|---|---|
| `Sandbox.create(template, timeout, envs, metadata)` | `client.sandboxes.create(template=, env=, metadata=, ttl_idle_s=)`;E2B 的 `timeout` ≈ `ttl_idle_s`;环境变量参数叫 `env` 不是 `envs` |
| `Sandbox.connect(id)` | `client.sandboxes.get(sid)` |
| `Sandbox.list()` | `client.sandboxes.list()` |
| `sbx.commands.run(cmd, cwd, envs, timeout, user)` | `sb.commands.run(cmd, cwd=, env=, timeout=, user=)`,同步返回 `CommandResult`(`exit stdout stderr`);`stream()` 对应 `on_stdout/on_stderr` 回调 |
| `sbx.files.read / write` | `sb.files.read`(bytes)/ `read_text` / `write`;另有 `list exists remove mkdir` |
| `sbx.pty.create(...)` | `sb.pty.open(user=, pty=)` → `Session`(`write` / `read`) |
| `sbx.set_timeout(n)` | `sb.set_timeout(ttl_idle_s=n)`(还有 `ttl_max_s`、`retain_paused_s`) |
| `sbx.kill()` | `sb.kill()`(别名 `delete`) |
| `sbx.pause()` / `Sandbox.resume` | `sb.pause()` / `sb.resume()` |
| `sbx.get_host(port)` | `sb.ports.url(port)`(经控制面转发,要带 Bearer)|

E2B 有、本 SDK 代码里没有的(已在 client.py / sandbox.py 确认无对应):异步客户端(`AsyncSandbox`);`commands.run(background=True)` / `commands.list` / `commands.kill` / `commands.connect` / `send_stdin`;`on_stdout` 之类回调参数;`pty.resize`;`files.watch_dir` / `files.get_info` / 批量 `write_files`,以及 `files.list` 返回元数据(这里只返回名字);`is_running` / `get_info` / `get_metrics`;`Sandbox.create` 作类方法(这里经 `Client`);`Template` 构建 DSL(这里用 `builds`)。E2B 官方 SDK 对本平台只能走第二端口的 E2B REST 兼容面([api/e2b.md](api/e2b.md)),其数据面(envd)不提供,命令和文件请用本 SDK。

## 我们多出的

| 能力 | 入口 |
|---|---|
| 检查点 / 原地回滚 / 从检查点 fork | `sb.checkpoints.*`、`sb.fork(checkpoint=)` |
| 预算 | `sb.budget(max_usd=, max_tokens=)` |
| 工单 | `sb.ticket(ticket)` |
| 收件箱 | `sb.inbox(text, kind=, actor=, thread=)` |
| 事件日志与等报告 | `sb.events(since=)`、`sb.wait_for_report()` |
| 出网策略与审批 | `sb.network.set()` + `NeedsApproval`;`client.network.requests/approve/deny`;`sb.network.decisions()` |
| 卷 | `client.volumes.*`,`create(volumes=[…])` 挂载 |
| 构建 | `client.builds.*`、`sb.commit()`、`templates/layers.everywhere` |
| 计费 | `client.billing.*`、`sb.usage()` |
| 项目与配额 | `client.projects.*` |
| 迁移 / 打包 / 差异 / 再落地 | `sb.migrate()`、`sb.pack()`、`sb.diff()`、`sandboxes.import_bundle()` |

## 例子

**上下文管理器**(退出即删)

```python
with c.sandboxes.create(mem=512, env={"MODE": "dev"}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
```

**流式输出**

```python
for ev, data in sb.commands.stream("for i in 1 2 3; do echo $i; sleep 1; done"):
    if ev == "done": print("exit", data.exit)
    else: print(ev, data, end="")
```

**PTY / 会话**

```python
with sb.pty.open(pty=False) as sh:             # 无 TTY 的长驻 shell,状态跨 exec 保留
    sh.exec("cd /tmp && export X=1")
    print(sh.exec("pwd; echo $X").stdout)
with sb.pty.open() as t:                       # 真 PTY,原始字节
    t.write("ls\n"); print(t.read(0.5).decode())
```

**从检查点 fork**

```python
sb.checkpoints.create("clean")
sb.commands.run("rm -rf /home/agent/*")
sb.checkpoints.rollback("clean")                       # 原地回去
a, b = sb.fork(n=2, checkpoint="clean", ttl_idle_s=120)  # 或分叉两份
```

**卷从 tar 建并挂**

```python
import io, tarfile
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t: t.add("harness/", arcname=".")
v = c.volumes.from_archive("harness", buf.getvalue(), size_mib=256)
sb = c.sandboxes.create(volumes=[{"id": v["id"], "mount": "/opt/harness", "ro": True}])
```

**结算账单导 CSV**

```python
print("today", c.billing.today())
open("statement.csv", "w").write(c.billing.statement_csv(since=t0, until=t1))
c.billing.settle(t0, t1)                               # 出账落库;c.billing.statements() 列历史
```

**放宽被 202**

```python
from cairnsandbox import NeedsApproval
try:
    sb.network.set(mode="audited", allow_hosts=["pypi.org"])
except NeedsApproval as e:
    print("等 owner 批准:", e.request["id"])
# owner 的客户端:
for r in c.network.requests():
    if r["status"] == "pending": c.network.approve(r["id"])   # 或 deny
```

**预算 + 等报告**

```python
sb.budget(max_usd=2.0); sb.ticket("fix-123"); sb.inbox("请修复 test_x")
rep = sb.wait_for_report(timeout=1800)                 # 超时为 None
```

## 命令行 `aios`

`pip install -e sdk/python` 后有 `aios` 命令(D63;零依赖,环境变量同 SDK,也可用 `--url / --token / --project`;`--json` 输出原始 JSON)。

```
cairn whoami                                   cairn status          # 公开状态,不要令牌;503 时退出码 2
cairn ls                                       cairn usage           # 今天与本期花费
cairn create --mem 512 --allow pypi.org        # 打印沙箱 id
cairn exec sb-1a2b -- python3 -V               # 退出码就是命令的退出码
cairn pause|resume|rm sb-1a2b
cairn cp ./a.txt sb-1a2b:/home/agent/a.txt     cairn cp sb-1a2b:/home/agent/out.txt ./
cairn checkpoint sb-1a2b before                cairn rollback sb-1a2b before
cairn keys ls | create ci --role viewer --days 30 | revoke key-…
cairn build py-data --from python:3.12-slim --run 'pip install pandas' --allow pypi.org   # 一步做出预热模板(见教程)
```
错误(404、403、402、429……)打印成一行 `cairn: <状态码>: <信息>` 并以 1 退出,不抛 traceback。

