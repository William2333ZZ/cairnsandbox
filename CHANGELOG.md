# 变更记录

格式:按日期倒序;沙箱云与 agent 两层分开写。设计理由在 [docs/decisions.md](docs/decisions.md)([索引](docs/decisions-index.md)),这里只写**使用者能感觉到的变化**。版本号与兼容承诺见 [docs/guide/compatibility.md](docs/guide/compatibility.md)。

## 控制台外壳跟工作台一致(D84)

### 沙箱云
- 修了一个竞态:删除沙箱时控制面在把 200 写给客户端**之后**才去掉路由,客户端收到 200 后立刻查路由偶尔还能查到(CI 上出现过一次 `route survived the delete`)。现在在响应头写出之前先去掉路由。
- E2B 兼容补验证:2 个节点下 Python 32 条、npm 20 条全过;多节点与节点挂掉后 evacuate 再用 SDK 通过;栈本身延迟与小规模并发的数字(`ops/e2b-multinode-check.py`、`ops/e2b-latency-raw.py`、`ops/e2b-latency.py`,结果在 `docs/research/e2b-conformance/`)。
- **E2B 兼容(D88)**:官方 E2B SDK(Python 与 npm 2.63.0)对着 E2B 口(18710)通过一致性用例(31 + 20 条):`commands.run`(流式输出、后台、stdin、PTY)、文件(读写、列目录、监视、20 MiB)、暂停与 connect、fork、快照、`update_network`、`get_metrics`。envd 协议做在 sandboxd(新模块 `envd.rs`,vsock 49983,没有新依赖);节点新增 `/sandboxes/<id>/envd/…` 流式中继;控制面按 `E2b-Sandbox-Id` 头路由、用绑定到单台沙箱的令牌鉴权、解析 `Start` 写审计;新增 v2 创建/列表/connect、fork、快照、网络、指标。进程跨检查点回滚仍在,`commands.connect` 可接回。要求带 envd 的基础镜像(重建;已有模板快照要重建)。矩阵与局限见 `docs/guide/api/e2b.md`。
- 多台真机:`deploy/docker-compose.node.yml` 与 `deploy/node.sh`(`init` / `up` / `register`)在另一台机器上起一个节点;`STORE_BIND` 让分块仓库绑到私网 IP;新增 `docs/en/multi-machine.md`。在一台机器上用 Docker 网桥地址当"私网 IP"验证过登记、远端创建、经仓库迁移、杀掉后 evacuate;两台物理机没验证。顺手修了:`TEMPLATE_POOL` 没设时默认空串会让节点启动就崩(现在默认 `[]`)。
- `deploy/multinode.sh`:单机多容器模拟多机。`up N` 起 N 个节点加控制面,`status`、`kill`、`start`、`partition`、`heal`、`evacuate`、`down`;内存预算按节点数自动分;`STORE_BW_MBPS` 给共享仓库限速。在 3 节点上实测:放置、节点断电后 evacuate 在别处恢复(文件内容还在)、节点回来后对账、断网与恢复。没做压力测试。
- 对外默认英文:`README.md` 改为英文(中文版是 `README.zh.md`,`README.en.md` 删除);控制台默认英文,界面右下角可切中文,已保存的选择不受影响;英文界面里小写的字段名首字母大写;README 里的两张截图换成新控制台的英文截图(真实控制面上拍的)。
- 控制台把顶栏加窄侧栏换成工作台那样的左侧栏:应用标记、项目选择、"新沙箱"、带图标的导航、底部的节点状态与账户行;窄于 1024 px 自动收成图标条,窄于 768 px 变抽屉;侧栏可手动收起并记住。
- 按钮、徽章、卡片标题、表头、输入框的尺寸与边框对齐工作台(`h-8` 控件、带边的状态徽章、安静的卡片标题)。
- 逐页检查后的小修:中文界面里一批英文表头与详情标签补了译文;成员表补了两列表头;沙箱详情的"事件"面板标题改正;webhook 示例地址去掉旧名。在真实控制面上再看一遍后,审计页的沙箱 id 不再换行、详情改成单行截断(悬停看全文)、表头补了"时间/操作"。再看了登录、注册、退出与令牌失效:退出后不再回到填着旧邀请码的注册表单;登录、注册和公开状态页现在跟随已保存的主题(之前只有控制台外壳里生效);注册失败的几种英文报错在中文界面里译成中文。
- 没改:路由、数据请求、权限;令牌与设计令牌本来就和工作台是同一套。

## 更名为 Cairn 与开源准备(2026-11-04,D82、D83)

项目从 AIOS 更名为 **Cairn**,按 Apache-2.0 开源。**不兼容的改名**(旧名不再识别):

| 旧 | 新 |
|---|---|
| Python 包 `aios_sandbox` / `pip install aios-sandbox` | `cairnbox` |
| JS 包 `@aios/sandbox` | `cairnbox` |
| CLI `aios`、`aios-sandbox-mcp` | `cairn`、`cairnbox-mcp` |
| 环境变量 `AIOS_URL`、`AIOS_TOKEN`、`AIOS_PROJECT`、`AIOS_HOME`、`AIOS_MODEL_*`…… | `CAIRN_*` |
| Prometheus 指标 `aios_*`、Grafana 面板 `aios-overview` | `cairn_*`、`cairn-overview` |
| API 密钥前缀 `aios_` | `cairn_`(旧前缀的密钥在新部署里不存在;自己的部署换密钥) |
| Webhook 头 `AIOS-Signature`/`-Event`/`-Delivery` | `Cairn-Signature`/`-Event`/`-Delivery` |

没改:guest 内部路径(`/aios/...`、`/run/aios/...`)与 agent 包目录 `aios/`,它们要重建镜像才能改,记在 ROADMAP。
新增 `LICENSE`、`NOTICE`、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`ROADMAP.md`、`.github/`、`scripts/bootstrap.sh`、`docs/en/`。
构建脚本的区域镜像(apt、pip、Go 代理、crates、内核源码)改成参数,默认走上游;注册表由 Redis 7 换成 Valkey 7.2(许可证原因,**compose 里的换镜像还没在实机起过**)。

## fork 与回滚更快(2026-11-04,D81)

### 沙箱云
- 同一检查点的 fork 多份、重复回滚不再每次重新合并内存:fork 3 份实测 5.2–11.4 s → 1.6–3.9 s,重复回滚约 0.9 s(几乎无磁盘数据时)。每个检查点第一次回滚或 fork 仍付一次合并。
- 回滚和 fork 的响应与节点审计行带 `phases`,看得出时间花在哪。
- 新增 `ops/dogfood-rollback.py`:对照测量延迟随内存还是磁盘增长。

## 审计链记 exec、模板任意大小(2026-11-04,D80)

### 沙箱云
- 审计链里每次 `exec` 和文件读写都有一行(命令行涂黑密钥形状、截 200 字符,带项目与调用者);`CP_AUDIT_EXEC=0` 关闭。
- 审计与审批记录里 deployer 令牌的调用者写 `deployer`,不再是 `default`。
- `template_key` 加 `"boot": "cold"` 可用模板的基础与层在任意内存下冷启动;大小不一致又没加它时,错误信息会告诉你两条路。

## 被拒有指引、OOM 有提示(2026-11-04,D79)

### 沙箱云
- 出口被拒时,HTTP 403 的原因短语说明哪个主机被拒、怎么申请(`PUT /sandboxes/<id>/network`)。
- 同步 `exec` 被内核 OOM 杀掉(退出码 137)时,响应带 `oom_killed` 与 `hint`。
- 放宽出口的申请与决定的审计行带 `project`,成员能看到自己项目的。

## 出口审批与审计实测(2026-11-04,D78)

### 沙箱云
- 新增 `ops/dogfood-egress.py`:成员在出口全关的沙箱里完成真实任务(控制面的 `govulncheck`),按失败申请放行、deployer 批准;结果与暴露的 6 个问题见研究文档第 12 节。上线指南加上"在沙箱里跑漏洞扫描"。

## 检查点 + fork 实测(2026-11-04,D77)

### 沙箱云
- 新增 `ops/dogfood-fork.py`;实测数字和适用场景写进研究文档第 11 节(没有代码行为变化)。

## 重建基础镜像(2026-11-04,D76)

### 沙箱云
- 新基础镜像 `debian-13-v2`(与 `debian-13` 并排,默认不变):带 `localhost`;`pack` 在内存放不下时改用沙箱磁盘,1 GiB 内存的沙箱也能打包大层。
- 重建步骤见运维手册 5b。

## 修缺口(2026-11-04,D75)

### 沙箱云
- `DELETE /templates/<名字>`(admin 任意,成员限自己项目提交的),名字随即可再用;Python / JS SDK 加 `templates.delete`。
- 用 `template_key=<模板名>` 建沙箱时,没写的 `layers` / `vcpu` / `mem` / `disk` 等自动取模板的,不用再照抄。
- 打包层过大时给出明确提示;基础镜像下次重建会带 `localhost`。

## 自己先用:开发回路进沙箱(2026-11-04,D74)

### 沙箱云
- 新增 `recipes/go-toolchain` 层与 `ops/dogfood-devloop.py`:`dev-go` 模板自带 Go 1.26、预热的编译缓存和运行中的 PostgreSQL,全部 Go 测试预热后约 12 秒。
- `node/build-layer.sh` 新增 `BUILD_MEM`,打包失败时会显示节点返回的原因。

## 存储与商业化路线(2026-11-03,D52)

### 沙箱云
- `fc-store` 新增 S3 兼容后端(Garage,`deploy/garage/`);`imgstore.py migrate` 迁移现有数据;`STORE_BACKEND=s3 ./run-node.sh store` 启用,不带即回退。
- 账户停用(D53):`POST /accounts/<a>/suspend`,暂停其运行中沙箱,成员只能读和清理;`/suspensions`、指标 `cairn_accounts_suspended`、控制台红色提示。
- 节点自我围栏(`FENCE_AFTER_S`)与可选自动接管(`AUTO_EVACUATE_AFTER_S`)(D54),默认关。
- JS/TS SDK `cairnbox`(`sdk/js/`,零依赖,fetch;与 Python SDK 同形状,8 个单测),文档 `docs/guide/sdk-js.md`。
- 预览域名(D55):`PREVIEW_DOMAIN` 设了后,`<端口>-<沙箱>.<域名>` 在浏览器直接打开沙箱里的服务,token 经 cookie 携带。
- 命名 API 密钥(单项目、member / viewer、可过期、可撤销)与只读角色(D56);控制台密钥区;两个 SDK 的 `keys` 接口。
- 沙箱实时指标(D57):节点每 5 秒采 cgroup 的 CPU / 内存 / 出站,`GET /sandboxes/<id>/metrics`;控制台指标页三条曲线。
- 滥用检测(D58):持续满载 CPU / 出站异常只标记不处置,`/abuse/signals`、指标与告警 `AiosAbuseSignal`。
- 预付额度(D59):账本、`/accounts/<a>/credit|balance|billing`,余额为零创建 402 并停用账户,充值即恢复;控制台用量页显示余额。
- 邀请制自助开户(D60):`/invites`、无需令牌的 `POST /signup`;邀请码带初始额度与试用配额;控制台登录页入口。
- 公开状态接口 `GET /status`(503 = 中断)与免登录状态页 `/console/status/`(D61)。
- 控制面 ↔ 节点可选 TLS(D62):节点 `TLS_CERT/TLS_KEY`,控制面 `NODE_CA_FILE`;`deploy/tls/gen-certs.sh`。
- 命令行 `aios`(D63):ls / create / exec / cp / checkpoint / keys / usage / status,SDK 之上的薄壳。
- 构建日志(D64):oci 构建的节点侧进度行,`GET /builds/<id>/log`,控制台按钮;运维账户页 `/console/admin/`(账户停用 / 充值 / 邀请码 / 滥用信号)。
- 默认价目表改为与 E2B / Daytona 公开价持平(D65;原来内存贵 4.5 倍、磁盘贵 68 倍,文档里的"约合"也算错了);定价对照见 docs/research/pricing-2026-11.md。
- 用量拆分 `GET /metering/projects/<p>/breakdown`(按沙箱 / 按日)与控制台用量页图表(D66);`deploy/garage/restore-drill.sh`。
- 可选的活跃 CPU 计费口径(D67):节点给出 `cpu_usec`,`vcpu_active_s` 价 > 0 才记,默认不变。
- Grafana 总览仪表盘 `deploy/grafana/cairn-overview.json`(12 块面板,测试保证只用已暴露的指标)。
- 账户下线 `DELETE /accounts/<a>`(D68;保留账本,名字不再发放)与 `ops/journey.py` 客户旅程演练。
- 一步做出预热模板(D69):`c.templates.build(...)` 与 `cairn build`(镜像 → 层 → 准备 → 提交,失败也清理)。
- 沙箱列表过滤与分页(D70):`?status / ?metadata=k:v / ?q / ?limit / ?offset` 与 `total`;两个 SDK 同步。
- 创建时的 `start_cmd`(D71):后台启动一次的命令,输出进 `/home/agent/start.log`,失败不让创建失败。
- Webhooks(D72):`/projects/<p>/webhooks`,事件签名推送、重试、连续失败自动关闭;连接时的 SSRF 防护(私网 / 回环 / 链路本地一律拒绝)。
- 依赖升级(D73):`govulncheck` 查出 pgx / go-redis / x/text 的漏洞,已升级(Go ≥ 1.25,构建镜像 golang:1.26-alpine)。
- 商业化差距与路线:[docs/research/commercialization-gap-2026-11.md](docs/research/commercialization-gap-2026-11.md)。

## 控制台重做(2026-11-03,D51)

### 沙箱云
- 新控制台 `console/`(Next.js,静态导出,控制面在 `/console/` 托管),独立于 agent 工作台;中英文、深浅色、按角色(operator / owner / member)显示不同导航。
- 页面:概览、沙箱(批量操作、终端、网络决策、检查点、事件、等价 SDK 调用)、网络审批、模板、卷、用量与账单(CSV)、成员、审计(核对哈希链)、设置、运维。
- 构建:`console/build.sh`(Docker);CI 增加类型检查与单测步骤。

## 0.1.0 之前(2026-11-03 一天内落地,D29–D49)

### 沙箱云
- 控制面改 Go、多副本;八个限界上下文(租户、生命周期、节点、镜像、状态、网络、计量、执行面)。
- 检查点与原地回滚、从检查点 fork、随迁移与 bundle 走;卷(独立生命周期,迁移带走);暂停保留期;WebSocket 端口隧道。
- 出口:closed / allowlist / audited,L7 规则(host 通配 / 正则、path、method)、凭证注入、每沙箱限额;成员放宽要 owner 批准。
- 租户:账户 ⊃ 项目 ⊃ 成员;配额;项目默认网络;令牌轮换与过期;请求体与速率上限。
- 计量与账单:CPU / 内存 / 存储 / 出站 / 模型 token;价目表版本化;结算账本、CSV。
- 调度:优先级类与抢占(先暂停、再结束 ephemeral)、节点余量、内存感知负载、cordon / drain / balance、层与模板分发。
- 镜像:OCI 与 docker save 归档成层(base 赢)、运行中沙箱提交为模板、Build 记录。
- 接入:E2B 形状的 REST(第二端口)、Python SDK `cairnbox`、MCP 服务、`POST /sandboxes/import`。
- 运维:`/metrics`、审计哈希链与 `/audit/verify`、`/audit`(按账户过滤)、compose 一键部署、`ops/bench.py` 与首批数字。
- 控制台:项目切换、概览、项目与成员、账单、卷、模板与构建(分发矩阵)、审计;沙箱详情的检查点与端口。

- 故障行为:注册表(Redis)不可达时按沙箱 id 的请求回 503(此前是 404);租户数据库不可达时创建回 503(此前是 404 "no project");控制面创建沙箱时把项目、优先级与元数据作为标签交给节点,**注册表丢失后由节点对账完整重建**(此前收养的沙箱落在 default 项目);创建时路由写入失败不再被吞掉。`/metrics` 增加依赖可达性、计量心跳、响应与限流计数、失联节点上的沙箱数;`deploy/alerts.yml`;运维手册 `docs/guide/operations.md`。
- 新接口字段:创建沙箱可带 `labels`(节点侧存放控制面的记账,≤ 64 对字符串),info 回 `labels`。

### agent 层
- 个人 harness 目录(人改立刻生效、机器改走 proposals/);宿主送目录进沙箱(tar 或只读卷);evolve 把接受的候选写成提案;`ops/home-tasks.py` 导回归题。

### 已知不兼容 / 要小心
- exec/stream 在线上的事件名是 `out` / `err`(旧文档写过 stdout / stderr)。
- 同一节点上的 `rbufsize` 改成无缓冲后,请求体要循环读满(已处理);自己写节点侧路由时用 `h._body`。
