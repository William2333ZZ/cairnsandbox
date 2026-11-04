# 沙箱(生命周期与执行面)

本文覆盖上下文 2(沙箱生命周期)和上下文 8(执行面):创建、查询、暂停 / 恢复、ttl、命令执行、会话(pty)、文件、fork、检查点与回滚、网络策略、端口暴露、预算 / 工单 / inbox / 事件 / diff、打包、迁移、导出导入。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md),沙箱本身见 [docs/003](../../003-sandbox.md)。卷在 [state.md](state.md),模板与构建在 [images.md](images.md),凭证与规则语法在 [network.md](network.md),账单在 [metering.md](metering.md)。

**路由约定**:`/sandboxes/<id>/…` 下除 `migrate`(admin)外的路径,控制面核对归属后原样转发给沙箱所在的节点(body、状态码、chunked 响应都透传)。别的账户的沙箱对你是 404。节点 down → 503(带 `durable`),节点不认识 → 503,转发失败 → 502。下面凡是"调用者"都指:能看见该沙箱的成员(本账户)或 admin。`Idempotency-Key` 头会透传给节点。

沙箱 info 的主要字段:`id, status(running|paused|…), template, class(persistent|ephemeral), vcpu, mem, ttl_idle_s, ttl_max_s, created, node, durable, boot_mode, layers?, network?, project, priority`,以及 `disk_used_mib, egress:{out,in}, checkpoints, volumes, template_snapshot, retain_paused_s, paused_at, env_keys`(`GET` 单台时更全)。其余字段见 CONTRACT §1。

## POST /sandboxes

调用者(成员或 admin)。创建沙箱,归属由 `X-Project` / `project` / `?project=` 决定(默认账户的 default)。

请求:
```json
{
  "template": "ubuntu-24.04",
  "class": "persistent",
  "vcpu": 2, "mem": 1024, "disk": 512,
  "ttl_idle_s": 300, "ttl_max_s": 3600,
  "qos": "normal",
  "priority": "interactive",
  "boot": "auto",
  "layers": ["L-xxxxxxxx"],
  "network": {"mode": "allowlist", "allow_hosts": ["pypi.org"], "allow_direct": [], "rules": [], "limits": {"requests_per_min": 600, "max_bytes": 104857600}},
  "retain_paused_s": 0,
  "env": {"NAME": "value"},
  "volumes": ["vol-id", {"id": "vol-id", "mount": "/mnt/data", "ro": false}],
  "metadata": {"k": "v"},
  "node": "node-a",
  "template_key": "my-template",
  "project": "web"
}
```
所有字段都可省。

| 字段 | 含义 | 默认 / 约束 |
| --- | --- | --- |
| `template` | 基础模板名 | `ubuntu-24.04`(合同示例值) |
| `class` | `persistent` 持久(开启对象存储时默认 durable,暂停时发 bundle)或 `ephemeral` 一次性 | 缺省 persistent |
| `vcpu` / `mem` | 规格;`mem` 单位 MiB(info 里的 `mem`) | 缺省值由节点定,见 CONTRACT §1 |
| `disk` | 可写层大小(MiB) | 配额准入时按 512 计;实际值以回包为准 |
| `ttl_idle_s` / `ttl_max_s` | 空闲多久自动暂停 / 总寿命上限(秒) | 见 CONTRACT §1 |
| `qos` | 节点调度类 | `priority=interactive` 时 `normal`,否则 `be` |
| `priority` | `interactive` / `batch` / `background`;batch / background 只放到还剩 2 个空位且 1 GiB 空内存以上的节点,不抢占别人 | `interactive`;其他值 → 400 |
| `boot` | `auto` / `cold` / `snapshot` | `auto` |
| `layers` | 叠在基础镜像之上的只读层 id | 无 |
| `network` | 出口策略(形状见 [network.md](network.md));没带则用 Project 默认([tenancy.md](tenancy.md)) | 无 → 节点默认 |
| `retain_paused_s` | 暂停(含空闲自动暂停)后保留多久再删;`0` = 一直留。durable 的暂停时已发 bundle,删后可再导入 | `0` |
| `env` | 每次 exec 的底层环境变量,≤ 64 对;请求自己的 `env` 盖在上面 | 无 |
| `volumes` | 卷 id 或 `{id, mount?, ro?}`;挂载规则见 [state.md](state.md) | 无 |
| `metadata` | 键值,记在路由上,节点不见 | 无 |
| `node` | 钉死放置节点(admin 之外是否可用见 CONTRACT §1);钉住的不受 cordon 影响 | 控制面按打分选 |
| `template_key` | 模板名或 16 位十六进制放置提示。给了模板名,没写的 `template` / `vcpu` / `mem` / `layers` / `disk` 等从模板取(D75);写了的必须与模板一致,否则 400(模板是某个确切大小的快照);要换大小,加 `"boot": "cold"`:用模板的基础和层冷启动,大小随你,但模板内存里的状态(运行中的数据库、预热的缓存)不会带来(D80) | 无 |

放置:暖池 / 模板快照优先,再看空位与空内存比例。interactive 找不到位子时,控制面先暂停一台 background(其次 batch)沙箱,再不行就结束一台 ephemeral 的 background / batch(审计 `preempt` / `preempt_destroy`);persistent 的只暂停不杀。

响应 **201**:沙箱 info(含 `node`、`project`、`priority`、`metadata`)。

| 错误 | 含义 |
| --- | --- |
| 400 | 网络策略不合法;priority 不对;`template_key` 与模板配置不一致 |
| 402 | `quota: daily budget` |
| 403 / 404 | Project 不是你的 / 不存在;卷不是本项目的 403;几个卷在不同节点 404 |
| 409 | `node` 与卷所在节点不一致 |
| 429 | `quota: sandboxes` / `quota: storage` / `no node with capacity`;卷已有写者 |
| 502 / 503 | 节点不可达;节点 draining |

```bash
curl -s -X POST $CP/sandboxes -H "Authorization: Bearer $TOKEN" -H "X-Project: web" \
  -d '{"template":"ubuntu-24.04","vcpu":2,"mem":1024,"ttl_idle_s":600,"env":{"MODE":"dev"}}'
```


**`start_cmd`**(D71,可选):创建成功后在沙箱里**后台启动一次**的命令,例如 `"python3 -m http.server 8080 --bind 0.0.0.0"`。以 agent 用户 `nohup sh -c '…'` 运行,输出进 `/home/agent/start.log`;响应里多一个 `start`:`{"started": true, "pid": "123", "log": "/home/agent/start.log"}`,或 `{"started": false, "error": …}`——**起不来也不会让创建失败**(沙箱已经在了,看日志自己处理)。只在创建时跑一次:暂停恢复靠内存保留进程,不会重跑;迁移 / 从 bundle 导入 / 检查点回滚后的进程状态跟着内存走,不重跑。要等服务就绪,自己轮询 `ports/<端口>/`。配合 [`ports`](#ports) 与[预览域名](../production.md)就是"一条命令开一个可访问的 web 服务"。
## GET /sandboxes

调用者。成员只列本账户的,admin 列全部。响应 `{"sandboxes": [info…], "total": N}`。

**过滤与分页**(D70,都可选,不带就是原来的"全部"):`?status=running`(可重复)、`?metadata=job:42`(可重复,**全部**满足才算)、`?q=文本`(在 id / 模板 / 节点 / metadata 里找子串,不分大小写)、`?limit=` 与 `?offset=`(用任何一个时按创建时间从新到旧排)。`total` 是过滤之后、分页之前的总数。例:`GET /sandboxes?status=running&metadata=team:red&limit=20`。

## GET /sandboxes/\<id\>

调用者。响应 info,多 `disk_used_mib`(实际分配)、`egress:{out, in}`(出口代理累计字节)、`checkpoints`(名字列表)、`volumes`、`template_snapshot`。404:不存在或不是你的。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/sandboxes/sb-123
```

## DELETE /sandboxes/\<id\>

调用者。响应 `{"deleted": "<id>"}`;控制面随后删路由。挂着的卷自动解挂。

## POST /sandboxes/\<id\>/pause · resume

调用者。响应:info。暂停期间只计磁盘;`resume` 后 `status=running`。暂停着的沙箱被端口请求访问时会透明恢复。

## POST /sandboxes/\<id\>/ttl

请求:`{"ttl_idle_s?": 600, "ttl_max_s?": 7200, "retain_paused_s?": 3600}`。响应:info。

```bash
curl -s -X POST $CP/sandboxes/sb-123/ttl -H "Authorization: Bearer $TOKEN" -d '{"ttl_idle_s":900}'
```

## POST /sandboxes/\<id\>/exec

调用者。同步执行一条命令。

请求:
```json
{"cmd": "python3 -V", "user": "agent", "cwd": "/work", "timeout": 60, "env": {"A": "1"}, "stdin": "…", "landlock": true}
```
- `cmd` 必填;`user`:`agent`(默认的低权限用户)或 `root`;`timeout` 单位秒;`env` 盖在创建时的 `env` 之上。
- `landlock`:`true` = 默认 harness 约束(全盘只读,可写 cwd / tmp / home,TCP 只许连出口代理 3128);或 `{"ro": [], "rw": [], "connect_ports": []}` 自定义。

响应:
```json
{"exit": 0, "stdout": "Python 3.12.3\n", "stderr": "", "timeout": false, "elapsed_s": 0.12}
```
- 进程被 SIGKILL(`exit` 为 137,经 shell 时;直接被信号杀掉的子进程是 -1)且内核日志里有刚发生的 `Out of memory: Killed process`,响应多两个字段:`"oom_killed": true`、`"hint": "govulncheck was killed by the kernel: out of memory in a 2048 MiB sandbox; create it with more mem"`(另有 `oom: {pid, process}`)。只有同步的 `exec` 有这个提示。(D79)
错误:404;503 节点 down。

```bash
curl -s -X POST $CP/sandboxes/sb-123/exec -H "Authorization: Bearer $TOKEN" -d '{"cmd":"uname -a","user":"root"}'
```

## POST /sandboxes/\<id\>/exec/stream

请求同 `exec`。响应是 chunked,每块一行 JSON:
```
{"event":"out","data":"…"}
{"event":"err","data":"…"}
{"done":true,"exit":0,"timeout":false,"elapsed_s":3.2}
```
线上的事件名是 `"out"` / `"err"`(Python SDK 把它们读成 stdout / stderr);最后一行是 `done`。

```bash
curl -sN -X POST $CP/sandboxes/sb-123/exec/stream -H "Authorization: Bearer $TOKEN" -d '{"cmd":"for i in 1 2 3; do echo $i; sleep 1; done"}'
```

## 会话(pty)

| 端点 | 请求 | 响应 |
| --- | --- | --- |
| `POST /sandboxes/<id>/sessions` | `{"user?": "agent", "pty?": true}` | **201** `{session, pty}` |
| `POST /sandboxes/<id>/sessions/<s>/exec` | `{"cmd": "…", "timeout?": 30}` | `{exit, stdout, …}` |
| `POST /sandboxes/<id>/sessions/<s>/write` | `{"data_b64": "…"}` | 见 CONTRACT §1 |
| `POST /sandboxes/<id>/sessions/<s>/read` | `{"wait?": 1}`(秒) | `{"data_b64": "…", "bytes": 12}` |
| `DELETE /sandboxes/<id>/sessions/<s>` | — | 见 CONTRACT §1 |

会话保留 shell 状态(cd、环境变量)。`pty:true` 时 `write`/`read` 收发终端字节(base64)。

```bash
S=$(curl -s -X POST $CP/sandboxes/sb-123/sessions -H "Authorization: Bearer $TOKEN" -d '{"pty":true}' | jq -r .session)
curl -s -X POST $CP/sandboxes/sb-123/sessions/$S/write -H "Authorization: Bearer $TOKEN" -d '{"data_b64":"'$(printf 'ls\n' | base64)'"}'
curl -s -X POST $CP/sandboxes/sb-123/sessions/$S/read  -H "Authorization: Bearer $TOKEN" -d '{"wait":1}'
```

## files

| 端点 | 说明 |
| --- | --- |
| `PUT /sandboxes/<id>/files?path=/abs/path` | body = 原始字节;响应 `{"bytes": n}` |
| `GET /sandboxes/<id>/files?path=/abs/path` | 响应 = 原始字节(`application/octet-stream`) |

读写的是**整个文件**:节点按 4 MiB 一块向 guest 读到读短为止;上限 **1 GiB**。`path` 必须是绝对路径。请求体还受 `CP_MAX_BODY_MIB`(默认 512)限制,超过 → 413。

```bash
curl -s -X PUT "$CP/sandboxes/sb-123/files?path=/work/a.txt" -H "Authorization: Bearer $TOKEN" --data-binary @a.txt
curl -s "$CP/sandboxes/sb-123/files?path=/work/a.txt" -H "Authorization: Bearer $TOKEN" -o a.txt
```

## POST /sandboxes/\<id\>/fork

调用者。从父沙箱克隆 n 份(父不动)。

请求:`{"n?": 1, "class?": "ephemeral", "ttl_idle_s?", "ttl_max_s?", "qos?", "checkpoint?": "name"}`。带 `checkpoint` 时克隆从那个检查点的内存与可写层出发。

响应 **201** `{"clones": [info…], "checkpoint": "…"}`;克隆自动成为路由,归父沙箱的 Project。错误:404 未知检查点。

```bash
curl -s -X POST $CP/sandboxes/sb-123/fork -H "Authorization: Bearer $TOKEN" -d '{"n":3,"checkpoint":"clean"}'
```

## 检查点与回滚

| 端点 | 请求 | 响应 |
| --- | --- | --- |
| `GET /sandboxes/<id>/checkpoints` | — | `{"checkpoints": [{name, note, created, paused_s, chain_len, upper_mib}]}` |
| `POST /sandboxes/<id>/checkpoints` | `{"name?": "clean", "note?": "…"}` | **201** 条目 |
| `POST /sandboxes/<id>/rollback` | `{"checkpoint": "clean"}` | 200 info + `{rollback_s, checkpoint}` |
| `DELETE /sandboxes/<id>/checkpoints/<name>` | — | 见 CONTRACT §1 |

- 在**运行中**打:保存设备状态、一层 Diff 内存、可写层的稀疏拷贝,打完继续跑,停顿几百毫秒。名字匹配 `[a-z0-9][a-z0-9_.-]{0,39}`,重名 409。
- 回滚是**原地**的:杀进程、换回可写层、从检查点的内存链恢复;id、目录、tap、IP 不变。
- 检查点引用的内存层被钉住,不被压缩或回滚回收。bundle(export / migrate / evacuate)带着检查点,导入后仍能 rollback。

```bash
curl -s -X POST $CP/sandboxes/sb-123/checkpoints -H "Authorization: Bearer $TOKEN" -d '{"name":"clean","note":"after setup"}'
curl -s -X POST $CP/sandboxes/sb-123/rollback    -H "Authorization: Bearer $TOKEN" -d '{"checkpoint":"clean"}'
```

## PUT /sandboxes/\<id\>/network

调用者:member 收紧立即生效,放宽走审批;owner / admin 直接改。

请求:`{"mode?": "closed|allowlist|audited", "allow_hosts?": […], "allow_direct?": […], "rules?": […]}`,`limits?` 也可带(规则语法见 [network.md](network.md))。节点按 key 合并,没带的 key 不动。响应 `{"network": {…}}`。

**审批流**:成员的请求若"放宽"了当前策略(含义见 [network.md](network.md)),控制面不推给节点,而是记一条待批请求,返回 **202**:
```json
{"request": {"id": "nr-…", "sandbox": "sb-123", "project": "web", "by": "bob", "network": {…}, "status": "pending"}, "note": "loosening the policy needs an owner: POST /network/requests/<id>/approve"}
```
owner / admin 随后到 `/network/requests` 批准或否决。收紧则直接 200。

错误:400 策略 / 规则不合法(控制面与节点都校验);404。

```bash
curl -s -X PUT $CP/sandboxes/sb-123/network -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"allowlist","allow_hosts":["api.github.com"]}'
```

## GET /sandboxes/\<id\>/metrics

实时指标(D57)。节点每 5 秒采一次沙箱 cgroup 的 CPU 与内存(外加出站累计字节),放在内存里的环形缓冲(默认一小时,720 点);`?since=<unix 秒>` 只要更新的点。响应:
```json
{"sandbox": "sb-…", "interval_s": 5.0, "points": [{"t": 1767225600.0, "cpu_pct": 12.5, "mem_mib": 183.2, "egress_out": 10240, "egress_in": 20480}]}
```
`cpu_pct` 是占**一个核**的百分比(2 个 vCPU 满载是 200);第一个点没有(没有上一次可比,为 null)。出站是累计值,要速率自己做差。沙箱暂停时没有点(曲线里是空档);节点重启后缓冲清空;没跑在 jailer 里的开发沙箱没有 cgroup,不出点。控制台沙箱详情的"指标"页用它画三条线。

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/sandboxes/$ID/metrics?since=$(( $(date +%s) - 300 ))"
```

## GET /sandboxes/\<id\>/egress

调用者。最近 200 条出站决定:`{"decisions": [{sandbox, method, scheme, host, port, path, allowed, rule, injected}]}`。

## ports

`ANY /sandboxes/<id>/ports/<port>/<path>?<query>`:反向代理到 guest 的 `ip:port`(明文 HTTP,整个响应)。

- 暂停着的沙箱会先透明恢复。
- 带 `Upgrade: websocket` 的请求握手后变成**双向字节隧道**(控制面劫持连接、拨节点、节点再拨 guest;guest 不回 101 就把它的回答原样透传)。写审计 `port_tunnel`。
- 错误:502 端口没人监听 / 节点不可达;400 坏端口。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/sandboxes/sb-123/ports/8000/health
```

## 预算、工单、inbox、事件、diff

这些是 agent-runtime 内核 ABI 的宿主侧(合同 §7)。

| 端点 | 请求 | 响应 |
| --- | --- | --- |
| `PUT /sandboxes/<id>/budget` | `{"max_tokens?": n, "max_usd?": x}` | `{"budget": {…}}` |
| `PUT /sandboxes/<id>/ticket` | `{"ticket": {id, short, text, scope, branch, base_branch, base_commit, cwd, rules, inputs, thread}}` | `{"ticket": {…}}`;写入 guest 的 `/run/aios/ticket.json` |
| `POST /sandboxes/<id>/inbox` | `{"kind": "…", "text": "…", "actor?": "…", "thread?": "…"}` | **201** `{"n": 3}`;追加到 `/run/aios/inbox.jsonl` |
| `GET /sandboxes/<id>/events?since=<seq>&max=` | — | `{"events": […], "last_seq": n}` |
| `GET /sandboxes/<id>/diff?cwd=/work/repo&base=main` | — | `{exit, stat, diff, stderr, base, cwd}`;沙箱内以 root 跑 `git diff <base>...HEAD` |

```bash
curl -s -X PUT $CP/sandboxes/sb-123/budget -H "Authorization: Bearer $TOKEN" -d '{"max_usd":2}'
curl -s "$CP/sandboxes/sb-123/events?since=0" -H "Authorization: Bearer $TOKEN"
```

## POST /sandboxes/\<id\>/pack

把该沙箱的可写层打成只读层。请求 `{"name": "my-layer"}`;响应 **201** 层元数据 `{id, name, bytes, …}`。之后可作 `layers:[id]` 创建新沙箱,见 [images.md](images.md)。

## 提交成模板:POST /builds {kind:"commit"}

把沙箱此刻的状态(内存 + 可写层)提交成命名模板,走构建接口,见 [images.md](images.md)。节点侧另有 `POST /sandboxes/<id>/commit`(内部)。

## POST /sandboxes/\<id\>/migrate

**仅 admin**。请求 `{"to?": "node-b"}`(省略则控制面选);响应见 CONTRACT §1,带卷时含 `volumes:[id]`。附着的卷先导到目标节点;卷还被别的沙箱附着则 **409** 且什么都不动;成功后源节点副本删除,路由改到新节点。带检查点。错误:403 非 admin。

```bash
curl -s -X POST $CP/sandboxes/sb-123/migrate -H "Authorization: Bearer $TOKEN" -d '{"to":"node-b"}'
```

## POST /sandboxes/\<id\>/export

调用者。导出 durable 沙箱的 bundle(带检查点)。响应字段见 CONTRACT §1;已有 bundle 用 `GET /store/bundles` 查([state.md](state.md))。

## POST /sandboxes/import

**仅 admin**。从 bundle 把一台 durable 沙箱重新落到一个节点,状态 `paused`,路由记到指定 Project。

请求:`{"bundle": "<bundle>", "node?": "node-b", "project?": "web"}`。响应 **201** info。错误:403;404 Project。

```bash
curl -s -X POST $CP/sandboxes/import -H "Authorization: Bearer $TOKEN" -d '{"bundle":"<bundle>","node":"node-b","project":"web"}'
```
