# 011 实现架构

这是开发者的代码索引,把 [002](002-domain.md) 到 [010](010-deployment.md) 里的概念对到 `prototype/fc-node/` 的模块、协议与测试。接口的逐字约定在 [`CONTRACT.md`](../CONTRACT.md)。

## 进程与数据流

```
web/ ──HTTP──▶ cp/ (fc-cp, Go) ──bearer──▶ nodeagent.py ──vsock──▶ sbx-runtime(guest)
                     │  /eval/*                 │ fcapi.py / fcctl.py        │ abi.sock ◀── aios/(harness)
                     ▼                          │ netmgr.py / egress_proxy.py
               evaluator.py ──▶ modelproxy.py   │ lazyfs.py ◀── imgstore.py(store)
                     │                          │ memchain.py / uffd_handler.py
                     ▼
               verify sandbox(经 controlplane)
```

- 控制面只做放置、路由、转发;评测器与(将来的)接单层经控制面创建沙箱,不直连节点。
- 节点代理是运行态权威;它持有每台沙箱的 VMM、网络、快照链、模板快照与池。
- 模型代理与分块仓库是独立进程,被评测器、节点分别调用。

## 模块

| 概念 | 模块 | 做什么 |
| --- | --- | --- |
| 节点代理 | `node/nodeagent.py` | HTTP 服务:准入、规格与模板键、创建 / 销毁 / 暂停 / 恢复 / fork / pack / 迁移,TTL,健康,事件与预算转发,模板快照与预留池,排空 |
| VMM 控制 | `node/fcapi.py`、`node/fcctl.py` | Firecracker API 客户端(驱动、快照、balloon、`disk` 扩容)与 jailer 启动 |
| 网络 | `node/netmgr.py`、`node/egress_proxy.py` | 每 VM tap、nftables、IP/MAC 分配;按域名放行的出口代理 |
| 内存 | `node/memchain.py`、`node/uffd_handler.py` | Diff 快照链与合并;userfaultfd 按需供页 |
| 层与仓库 | `node/imgstore.py`、`node/lazyfs.py` | 分块仓库服务与客户端(HEAD/PUT 去重、bundle 清单、GC;后端可换:本地目录或 S3 兼容桶,`migrate` 迁移,D52);FUSE 按需加载与共享缓存。`deploy/garage/`(Garage 单节点配置与 `bootstrap.sh`)是 S3 后端的部署;`deploy/tls/gen-certs.sh` 生成节点 TLS 证书(D62);`deploy/garage/restore-drill.sh` Garage 备份还原演练(D66);`tests/unit/test_imgstore_backends.py` 让两种后端跑同一个场景 |
| guest 客户端 | `node/sbxclient.py` | 节点侧的 vsock NDJSON 客户端 |
| 控制面(Go) | `cp/main.go`(接线)+ `cp/internal/` 按 [002 沙箱侧的领域](002-domain.md) 的上下文分包:`tenancy/tenancy.go`(1 租户:Account / Project / Member / Quota,SQL;`tenancy/suspend.go` 账户停用,D53;`tenancy/keys.go` 命名 API 密钥与 viewer 角色,D56;`tenancy/invites.go` 邀请码与自助开户,D60;`tenancy/delete.go` 账户下线,D68;`tenancy/billing.go` 计费方式(后付 / 预付),`metering/credit.go` 额度账本,D59;`metering/breakdown.go` 用量按沙箱 / 按日拆分,D66;`db/testing.go`(测试用数据库:默认 SQLite;设 `CAIRN_TEST_PG=postgres://…/postgres` 就给每个测试一个一次性的 Postgres 库,同一套测试证明两种 SQL 都通;在开发机上跑过 tenancy / httpapi / metering 全过);`nodeclient/client.go`(+`client_test.go`)控制面到节点的 HTTP 客户端,https 节点按 `NODE_CA_FILE` 校验,D62;`webhook/webhook.go`(+`webhook_test.go`)项目事件的签名推送与 SSRF 防护,D72;`abuse/abuse.go`(+`source.go`、`abuse_test.go`)滥用信号:持续满载 CPU / 出站洪泛只标记不动手,D58;旧 `/users` 是 default 账户的成员)、`lifecycle/lifecycle.go`(2 生命周期:路由、创建与放置、列表、迁移、evacuate、租约下的对账,`WatchFleet` 挂到节点上线)、`fleet/node.go` `fleet/health.go` `fleet/place.go`(3 节点:登记、健康轮询与回调、容量、cordon、优先级类的余量、打分放置、租约;drain 与 balance 在 lifecycle 里,因为它们搬的是沙箱)、`images/templates.go` `images/builds.go`(4 镜像:模板快照建一处分发各处;Build 对象(SQL):oci 拉成层、commit 提交成模板,异步跑在节点上;各节点模板与层的目录)、`state/bundles.go` `state/volumes.go`(5 状态:分块仓库里的 bundle、gc;卷的目录:归项目、住哪个节点、在节点上建与删、附卷的沙箱钉到该节点)、`network/policy.go` + `network/requests.go`(放宽要审批:Relaxes 与 Request 表;`requests_test.go`)+ ``(6 网络:NetworkPolicy 与 L7 规则的校验、凭证推到各节点保险库)、`metering/metering.go`(7 计量:UsageEvent、PriceList、Statement、每分钟的 tick 与模型代理增量,SQL)、`exec/forward.go`(8 执行面:转发到所属节点、流式逐块、fork 的克隆成路由)、`e2b/api.go`(8 的另一张皮:E2B 形状的 REST 在第二个端口,X-API-Key,create / list / get / kill / pause / resume / timeout / templates 映射到 lifecycle 与 images);接口层:`httpapi/auditcalls.go`(exec 与文件读写进审计链:命令行涂黑并截断,调用者写 `deployer` 而不是 `default`,D80);`httpapi/server.go`(CONTRACT §1 的路由、鉴权、静态工作台、`/eval/*` `/intake/*` 转发、事件流;不拥有数据);基础设施:`db/db.go`(SQL:SQLite 或 Postgres,一套方言,迁移幂等)、`registry/registry.go`(共享存储,文件或 Redis,各上下文只取自己那片接口)、`nodeclient/client.go`(到节点的 HTTP)、`audit/audit.go`(每副本的审计);`cp/Dockerfile`(goproxy.cn + 模块缓存挂载,构建里跑测试)、`cp/go.mod`、`cp/go.sum` | 依赖方向:httpapi → 各上下文 → fleet / nodeclient / registry;fleet 不知道路由,lifecycle 以回调挂上去;上下文之间不共享表(D32)。D31 之前是 `node/controlplane.py` |
| SDK(Python) | `sdk/python/cairnbox/`:`cli.py`(`aios` 命令行,SDK 之上的薄壳,`tests/unit/test_cli.py`,D63);`client.py`(Client、集合:sandboxes / volumes / templates / layers / builds / billing / projects / network、错误类)、`sandbox.py`(Sandbox:commands / files / pty / ports / checkpoints / network、budget / ticket / inbox / events)、`mcp.py`(stdio 的 MCP 服务:把沙箱当工具给 Claude Code / Codex 这类 agent,零依赖,D45);`sdk/python/examples/quickstart.py` 对着真控制面走一遍(CI 跑);`tests/unit/test_sdk.py` 用假控制面核对路径、body、头与错误映射。零依赖,E2B 有的同形,多出来的是我们的(D43) |
| 告警 | `deploy/grafana/cairn-overview.json`(Grafana 总览仪表盘,同一批指标);`deploy/alerts.yml`(Prometheus 告警规则,每条带严重度、持续时间与处置);`tests/unit/test_alerts.py` 保证规则里引用的指标都在 `/metrics` 里、每条都有处置(D50) |
| 部署与数字 | `ops/journey.py`(客户整条旅程的真机演练:邀请、开户、密钥、沙箱、额度、停用、下线,D68);`ops/demo-account.py`(一个带活动的一次性演示账户,截图与演示用);`ops/dogfood-rollback.py`(检查点、回滚、fork 的延迟随内存还是磁盘增长的对照测量,D81);`ops/dogfood-devloop.py`(自己的开发回路在沙箱里:准备 `dev-go` 模板、跑 Go 测试并计时,D74);`ops/dogfood-fork.py`(检查点 + fork 的实测:串行 / 回滚 / 并行分片 / 隔离,D77);`ops/dogfood-egress.py`(成员在出口全关的沙箱里完成真实任务,走审批与审计,D78);`recipes/go-toolchain`(Go 1.26 + 预热模块缓存 + PostgreSQL 的环境层配方,D74);`deploy/docker-compose.yml`(整套一次起齐,与 run-node.sh 服务一一对应;密钥经 compose secrets 文件:CP_TOKEN_FILE / CP_DB_PASSWORD_FILE)、`deploy/.env.example`、`deploy/make-secrets.sh`、`deploy/README.md`(第一次、升级、备份、第二个节点);`ops/bench.py` 量沙箱层的数字(创建 / exec / 暂停 / 恢复 / 检查点 / 回滚 / fork 时延,每台内存,密度爬坡),结果在 `docs/research/benchmark-2026-11.md`(D44) |
| 模型代理 | `node/modelproxy.py` | OpenAI 兼容;每沙箱令牌与预算;价格表;mock 与 upstream 后端;密钥从文件读 |
| 评测器 | `eval/evaluator.py` | 任务集与导入检查、版本、批次、Run;CP 与本地两种后端;统计(avg@k、δ、配对 bootstrap、McNemar);级联;作弊信号;事件拉取;SQLite |
| 任务集 | `eval/tasksets/<name>/` | 格式见 CONTRACT §6;`basic` 只作管线自检;`polyglot-py` 由 `eval/import_polyglot.py` 从 Aider polyglot-benchmark 导出;`workbuddy-code` 由 `eval/import_workbuddy.py` 从 WorkBuddy-Bench 导出(不进仓库) |
| 版本循环驱动 | `eval/mine_history.py` | 从仓库自己的提交历史挖任务:有 Python 测试又有 Python 源码的提交 → 提交前的工作区、提交带来的测试(隐藏)、源码改动(参考);按时间 60/20/20 分 evolve / validate / test;交给评测器的导入与自检 |
| `eval/evolve.py` | 父代 → 元 agent 提案(dev 沙箱)→ critic → 构建候选层 → 登记版本 → 级联比较 → 入档;`evolve.json` |
| 运行时 | `guest/sbx-runtime/src/main.rs` | sandboxd:vsock 监听、19 个操作、PTY 会话、降权、pack、冻结 |
| 内核 ABI | `guest/sbx-runtime/src/abi.rs` | `/run/aios/abi.sock`;SO_PEERCRED;hello / event / budget_get / budget_charge / checkpoint / report / ticket_get / inbox;事件日志;宿主侧 ticket_set / abi_input |
| Landlock | `guest/sbx-runtime/src/landlock.rs` | 规则集构造、ABI 探测、默认 harness 约束、显式策略 |
| guest init | `node/guest/fc-init` | 挂只读层与可写层、组 overlay、switch_root、起 runtime;`node/guest/sandboxd.py` 是 Python 版回落 |
| Cairn(agent runtime) | `aios/__init__.py`、`agent.py`、`middleware.py`、`model.py`、`env.py`、`run.py`、`abi.py`、`config.json`、`memory.md` | mini-swe-agent 裁剪:一个 bash 工具、一个循环;stdlib 模型客户端;ABI 客户端(socket 不存在即空操作) |
| 个人 harness 目录 | `aios/home.py` | 一个人带进每件事的目录(`--home` / `$CAIRN_HOME` / `/aios/home`):config.json 覆盖、memory.md、rules/、prompts/、skills/、middleware/;tasks/ 归评测器、proposals/ 是机器的提案,运行时都不读;`check` 不过就不开跑;`apply` 是机器提案进门的唯一路;摘要进轨迹([004](004-guest.md) 个人 harness 目录,D29) |
| 配方 | `recipes/kernel-6.18/`、`base-debian-13/`、`sbx-runtime/`、`agent-langs/`、`agent-harness/`、`dev-tools/` | 每层的构建脚本或 `recipe.sh`;模板 JSON 在 `recipes/templates/` |
| 层构建 | `node/build-layer.sh`、`node/build-dev-layer.sh`、`node/build-rootfs.sh`、`node/smoke-boot.sh` | 在构建沙箱里跑配方并 pack;旧的整盘构建;单机启动烟测 |
| 工作台 | `web/src/kernel`(插件注册表)、`web/src/layout`(三栏外壳)、`web/src/plugins/*`(功能插件;`cloud/` 是沙箱侧对象的页面:账户 / 项目 / 配额 / 成员、账单与价目表、卷、模板与构建;`nodes/` 带 cordon / drain / balance / evacuate)、`web/src/store`、`web/src/api`、`web/src/ui`;`web/DESIGN.md`、`web/scripts/design-check.mjs`、`web/vite.config.ts`、`web/tailwind.config.js`、`web/postcss.config.js`;`web/legacy/`(旧原生 JS 页面,含 `legacy/index.html`、`legacy/app.js`、`legacy/style.css`、`legacy/mock-server.py`) | React + TS + Vite,Mac 上构建到 `web/dist/`,控制面提供 |
| 启动 | `run-node.sh`、`node/Dockerfile`、`scripts/lib/verify.sh`、`scripts/release-artifacts.sh`(发布产物打包,配合 `bootstrap.sh --artifacts`)、`tests/unit/test_verify_sh.py` |
| `scripts/bootstrap.sh` | 服务启动与镜像 |
| CI | `.github/workflows/ci.yml` | Go、Python 单测与 JS SDK 构建(不需要 KVM);端到端套件见 [010](010-deployment.md) |
| Agent 服务(旧名接单层) | `intake/intake.py` | Policy(Gate 代码 + 版本哈希)、Store(SQLite)、Engine(receive / evaluate_due / accept / assignments / report / outbox / follow_prs)、Gitea(Source 轮询与中继)、HTTP;`node/test-intake.py` 端到端 |

## 协议

| 协议 | 位置 | 要点 |
| --- | --- | --- |
| 控制面 / 节点 HTTP | CONTRACT §1、§2 | 同一组路径;控制面转发;流式为 chunked,每块一行 JSON |
| 评测服务 HTTP | CONTRACT §3 | 任务集、版本、批次、Run |
| 接单层 HTTP | CONTRACT §8 | attention、requests、triggers、threads、decisions、tickets、policy |
| 模型代理 | CONTRACT §4 | OpenAI 兼容;响应头回成本 |
| harness 调用约定 | CONTRACT §5 | `/aios/bin/aios run …`;退出码;轨迹格式 |
| 内核 ABI v2 | CONTRACT §7 | NDJSON over unix socket;report / inbox / ticket_get;宿主侧 ticket_set / abi_input |
| sandboxd vsock | `main.rs` 与 `sbxclient.py` | NDJSON;请求 `{op, …}`;流式事件;错误串 "类型: 信息" |
| 任务集目录 | CONTRACT §6 | meta、prompt、init、verify、reference |

## 状态目录

| 目录 | 内容 |
| --- | --- |
| `state[-n]/` | 节点:VM 目录(jail 根)、模板快照、池、`node-token`、`layers-built.jsonl`、`*-layer-id.<template>` |
| `cp-state/` | 控制面:`routes.json`、`users.json`、`nodes.json`(文件注册表时)、`audit.jsonl`(每副本追加) |
| `registry-data/` | 副本共享的 Redis(fc-registry,appendonly) |
| `store/` | 分块仓库:块、bundle 清单 |
| `model-state/` | 令牌与用量 |
| `eval-state/` | 评测器 SQLite、Run 产物、导入检查结果 |
| `intake-state/` | 接单层 SQLite(trigger、thread、decision、ticket、assignment、outbox、policy_versions、kv) |
| `images/` | 内核、基础镜像、runtime 二进制、下载的解释器(不进仓库) |

## 测试

| 测试 | 覆盖 |
| --- | --- |
| `tests/unit/test_nodeagent_spec.py` | 规格解析、模板键、`disk` |
| `tests/unit/test_netmgr.py`、`test_egress_policy.py` | 网络分配;域名放行规则 |
| `tests/unit/test_evolve.py`(候选部分) | 人写的候选(`eval/candidates.py`:`nudge`、`ask-paragraph`)是整份 harness 加一处有说明的改动,清单能过 `parse_manifest` |
| `tests/unit/test_prompts.py` | 团队写的提示段落(沟通、范围、交付):版本、冲突、上限、默认变了的提示、只是建议不影响宿主的检查(`intake/prompts.py`) |
| `tests/unit/test_threads.py` | PR 属于开它的工单、只读的上下文订阅、共享 Trigger 的线程互相关联(dsh-superagent 001/003) |
| `tests/unit/test_harvest.py` | 采集:arXiv 每日新论文列表与开源 harness 仓库的提交、发布,打分、去重、写摘要,不用模型(`eval/harvest.py`) |
| `tests/unit/test_digest.py` | 一次运行的整体形状(读了多久才写、有没有跑检查、绕圈)不用模型读出来,给元 agent 看(`eval/digest.py`) |
| `tests/unit/test_interfaces.py` | 挖出来的任务把测试用到而工作区还没有的名字写进提示(`eval/interfaces.py`) |
| `tests/unit/test_modelproxy.py` | 模型代理按缓存命中计价(命中价是未命中的 1/50)、两种命中字段、一次真实经过代理的调用 |
| `tests/unit/test_memchain.py`、`test_uffd_pagemap.py` | 快照链合并;页归属 |
| `tests/unit/test_lazyfs_slicing.py` | 按需加载的块切分 |
| `tests/unit/test_eval_stats.py`、`test_eval_local.py` | δ、配对统计、接受判定;本地后端整流程(无工具链时跳过) |
| `tests/unit/test_aios_seed.py` | 种子 harness 的循环与模型客户端 |
| `tests/unit/test_home_host.py` | 宿主送目录:空目录不送、送的是运行时读的文件且只读、摘要与运行时一致并进 ticket / Assignment、人的改动随下一次派工、tasks 与 proposals 不出宿主、路径围在运行时读的范围内、一个用户一个目录多个 agent 共用(`intake/home.py`) |
| `node/test-ports.py` | 节点 e2e:沙箱里起的 HTTP 服务经 /ports/<port>/ 可达、查询串透传、没人听 502、坏端口 400、暂停的沙箱透明恢复 |
| `node/test-images.py` | 节点 e2e:运行中的沙箱提交成有名字的模板(内存与进程都在)、从它热启动、配置不一致 400、16 位键只是提示;从 OCI 镜像拉成层并叠在基础之上、缺镜像 502 |
| `node/test-egress.py` | 节点 e2e:凭证保险库(名字可见、值不可见)、L7 规则(拒绝 / 带凭证放行 / 兜底放行、第一条命中为准)、明文 HTTP 注入且沙箱看不到值、closed 下无规则的主机 403、决定日志、audited 模式、坏规则 400 |
| `node/test-vol.py` | 节点 e2e:建卷、附 rw 写、读者不能和写者共存、删挂着的卷 409、两个读者共享且只读生效、暂停恢复保持挂载、释放后删除 |
| `node/test-ckpt.py` | 节点 e2e:运行中打检查点、链增长、原地回滚(磁盘与内存都回去、id 与网络不变)、来回回滚、与暂停 / 恢复共存、删一个检查点不伤另一个(层被钉住) |
| `tests/unit/test_home.py` | 个人 harness 目录:规则按文件名序且注释不算、config 合并、提示替换且 instance 必须带 {{task}}、技能按名字与说明列出、自己的中间件、整次运行带着规则/笔记/摘要、坏目录不开跑、机器提案只在 apply 后生效且坏提案原样放回(`aios/home.py`) |
| `tests/unit/test_layers.py` | 分层:agent 层只走 HTTP / SDK 到沙箱云,沙箱层不 import agent 层 |
| `deploy/node.sh`、`deploy/docker-compose.node.yml` | 在另一台机器上起一个节点并登记到控制面(D87) |
| `deploy/multinode.sh` | 单机多容器模拟多机:`up N` 起 N 个节点加控制面,`kill` / `partition` / `evacuate` 注入故障(D86) |
| `guest/sbx-runtime/src/envd.rs`、`guest/sbx-runtime/src/inflate.rs`(gzip 解码)、`cp/internal/e2b/envd.go`、`ops/e2b-conformance.py`、`ops/e2b-conformance.mjs`、`ops/e2b-multinode-check.py`、`ops/e2b-latency.py`、`ops/e2b-latency-raw.py`、`ops/e2b-matrix.sh`(SDK 版本矩阵)、`ops/e2b-code-interpreter-conformance.py`、`recipes/code-interpreter/server.py`、`recipes/code-interpreter/build-template.py`(code-interpreter 模板与服务)、`recipes/ci-pylibs`(numpy/pandas/matplotlib 等离线包层,`fetch-wheels.sh` 取 wheel)、`.github/workflows/e2b-conformance.yml`(自托管 KVM 机器上手动跑,从没跑过) | E2B envd 兼容:沙箱里的 Connect/REST 服务(vsock 49983)、控制面的按头路由与审计解析、官方 SDK 一致性用例(D88) |
| `scripts/bootstrap.sh` | 新机器上的一键准备:Firecracker、镜像、内核、runtime、基础镜像、密钥、控制台、分块仓库(开源发布设计 §7 P5) |
| `tests/unit/test_docs_index.py` | 文档:每个模块都在本文索引里;编号文档只向前引用;无断链 |
| `tests/unit/test_commands.py` | 预算改动被应用并确认且不开单、范围外被拒、只是提到预算的请求仍是请求、清单来自自己的状态、陌生人不能改 |
| `tests/unit/test_isolation.py` | 两个用户共用一个部署时,纠正、规则及其检查、读数、意图提醒各归各的 |
| `tests/unit/test_review.py` | 独立审查:CI 与密钥拦下且要二次确认、删测试与测量代码只提醒、向外发的密钥 / 地方 / 链接 / 来源、预算与例行、后台模型只升不降且失败只记问题 |
| `tests/unit/test_corrections.py` | 纠正:插话成规则并带进下一件事、回答提问与机器消息不算纠正、否决与关 PR、模型提炼与失败回落、规则按仓库、harness 与评测器措辞一致、纠正过的工单挖成依赖题 |
| `tests/unit/test_mine_history.py` | 历史挖任务:哪些提交算题、题由什么组成(提交前的工作区、隐藏测试、参考)、后来的工作留作测试集 |
| `tests/unit/test_eval_tasks.py` | 任务的种类:问题与 blocker 的匹配、Ask-F1、obstacle 题是多轮对话且验收最后一次的工作区、dependent 题带规则与对照臂、验收脚本按测试文件给部分分(真跑 pytest)、导入记录 kind / rules / blockers、批次摘要按类别分 |
| `cp/internal/db/db_test.go`、`cp/internal/tenancy/tenancy_test.go`、`cp/internal/metering/metering_test.go`、`cp/internal/network/policy_test.go` | 控制面的 SQL 侧(SQLite 跑)与网络策略校验:迁移两次、$1 占位;账户 / 项目 / 成员 / 配额、旧用户导入一次、项目归属与跨账户 403;价目表版本化、按价记费、模型代理增量跟着游标走、未知沙箱落 default、代理挂了不算错 |
| `cp/internal/registry/registry_test.go`、`cp/internal/httpapi/auditcalls_test.go`、`cp/internal/httpapi/server_test.go`、`cp/internal/fleet/place_test.go`、`cp/internal/audit/audit_test.go`(哈希链:改一行、删一行都从那里断) | 控制面(Go,Redis 用 miniredis):两种注册表的路由 / 用户 / 节点 / 租约、注册表重启后恢复、文件损坏即空;对假节点的创建 → 路由 → 转发 → 删除、流式逐块、准入分散与 429、模板快照优先、用户与鉴权、两副本共用一个注册表、租约下的对账、死节点 503 与 evacuate、cordon / drain / balance / 优先级余量、E2B 形状的创建 / 列表 / 暂停 / 超时 / 删除、镜像档案上传成构建、静态与转发、事件流、账户 / 成员 / 配额的权限与沙箱配额 429 / 当日预算 402、成员只见本账户、卷归项目并把沙箱钉到卷所在节点;`go test -race`,在镜像构建里跑 |
| `tests/unit/test_agent_interrupt.py` | harness 插话:人的话打断进行中的模型调用并重规划,被放弃的调用费用延后计入 |
| `tests/unit/test_import_workbuddy.py` | WorkBuddy 导入器的工作区大小上限(由 agent 在工单 82495839 里写的) |
| `tests/unit/test_intake_engine.py` | 接单引擎(假控制面):Gate、accept、派工、Report、恢复、停止、PR 跟随、策略版本 |
| `tests/unit/test_evolve.py` | 版本循环驱动:diff 级 critic(验证路径、任务名、篡改、体积)与 Thompson 父代选择;改动清单只留元 agent 能知道的、预测与结果按任务对照并找出丢掉的稳定通过、更新载荷带原因与归因 |
| `web/src/kernel/kernel.test.ts` | 前端插件内核(vitest):依赖顺序、缺依赖跳过、停用撤回、服务唯一、事件、环检测 |
| `node/test-intake.py` | 接单层端到端:经控制面 `/intake`,Gate、策略版本、工作台请求 → 沙箱 → ABI report、恢复、人工停止 |
| `node/test-node.py` | 节点 HTTP 端到端:创建、exec、会话、文件、TTL、暂停恢复 |
| `node/test-net.py` | 网络策略:直连被丢、代理放行、动态更新 |
| `node/test-layers.py` | 层叠放、pack、父子 |
| `node/test-jail.py` | jailer:chroot、uid、cgroup、权限 |
| `node/test-snap.py` | Diff 链、模板直启、池、uffd、排空重启 |
| `node/test-lazy.py` | 按需加载:拉取字节、画像预取 |
| `node/test-cp.py` | 控制面:放置、路由、迁移、evacuate、对账、流式透传 |
| `node/test-template.py` | 模板:冷启动、overlay、用户、Landlock、io_uring、user ns、EROFS、出口、ABI、事件、预算、pack、快照启动 |
| `node/test-eval.py` | 产品层端到端:工作台、评测器、导入检查、基线、比较、ABI 事件 |

每个端到端脚本退出时打印 `RESULT ok=N fail=M`。

## 工作清单

不分期(001):一次一件事,做完一件再做下一件。每件事的"做完" = 代码、单元测试、CI 镜像里通过、部署到开发机、真实走一遍、契约与文档同步。顺序按用户下一次打开工作台最先感觉到什么排:

1–10 已做完;11–16 是 [2026-11 的调研](research/landscape-2026-11.md)定下的方向(D17),也已做完(2026-11-02):代码、单元测试、部署、在开发机上用真实模型真实走了一遍(`ops/corr-walk.py`、`ops/review-walk.py`、`ops/ask-walk.py`),走的过程里发现并修掉的问题记在 [D18](decisions.md)。`ops/ui-demo.py` 用假沙箱与假代码托管起一个带种子数据的服务,不接部署就能在工作台里看到这些新东西。`ops/home-tasks.py` 把个人 harness 目录里的 `tasks/<仓库>/` 导进评测器(题集 `home-<用户>-<仓库>`,D29)。`ops/decisions-index.py` 从 decisions.md 的标题生成 `docs/decisions-index.md`。其余 ops 脚本:`ops/gate-walk.py`(接受门真走一遍)、`ops/user-eval.py`(按用户的评测)、`ops/after-topup.py`(充值后的驱动,D23)。

| # | 事 | 做完的样子 |
| --- | --- | --- |
| 1 | Agent 服务拆成模块包 | `intake/` 下按职责分文件(见下),行为与测试不变 |
| 2 | 用户进数据模型 | 每张表带 `user` 列,kv 按用户分键,查询永远带用户;现在只有一个用户 `default` |
| 3 | 从用户的事挖任务 | 合并且带测试的 PR 自动变成该仓库任务集里的一道题;评测器能导入;迭代驱动在该任务集上跑 |
| 4 | 通知 | 浏览器通知(做完 / 卡住 / 要决定),未读在侧栏 |
| 5 | 接入页与 Connector 抽象 | Connector 接口(读:触发;写:回话、PR、合并);GitHub 实现与 Gitea 同形;接入页列出、断开 |
| 6 | 插话打断 | 人的消息在当前模型调用返回前就送到,"停"立即 |
| 7 | 多用户 | 部署者新建用户(令牌只存哈希),每个用户自己的 Engine 与 Store;Postgres 以后再说 |
| 8 | 聊天工具 Connector | 飞书 / Slack / 通用 webhook 进出;密钥在宿主侧;飞书与 Slack 只按文档对接,未真实联调 |
| 9 | 分屏 | 两件事并排看(`?split=`,两片叶子,不嵌套) |
| 10 | 团队与多个 agent | 每个用户可开按岗位命名的 agent(自己的事、例行、记忆),"@名字"指派;agent 之间自己传活还没有 |
| 11 | ✓ 纠正进记忆与考题 | 用户的纠正(插话、退回、否决)成为记忆里的一条规则,并成为该仓库任务集里一道带依赖的题;下一次同类事不再错 |
| 12 | ✓ 用户任务集的三种题 | 依赖题(前面做过的事里有可复用知识)、障碍题(埋着缺信息 / 歧义 / 矛盾,看该问时问了没,Ask-F1)、长题(按子任务给部分分);评测器支持部分分 |
| 13 | ✓ 该问就问的尺子 | question / approval 的精度与召回进评测;更新说明里报它 |
| 14 | ✓ 进化的变更清单与归因 | 每个候选版本带变更清单(改了哪个部件、为什么、预计影响哪些任务);评测后对照预计与实际写进 history;回退能回答"退到哪一版、为什么" |
| 15 | ✓ 不可信内容与独立审查 | 从渠道进来的内容(PR 评论、聊天、网页)在 ticket 里标不可信;不可逆动作(合并、发消息、超预算)由进化循环之外的独立检查审,不由 agent 自己判断 |
| 16 | ✓ agent 之间的接缝 | 002 写清一段对话的所有者、交接时传什么与传回什么、写动作只能由所有者做;之后才做 agent 互传 |

Agent 服务的模块(清单第 1 条之后的样子):

| 文件 | 职责 |
| --- | --- |
| `intake/intake.py` | 入口:main、serve;把下面各模块的名字重新导出,脚本与测试只认这一个 |
| `intake/home.py` | 个人 harness 目录的宿主侧:`<state>/homes/<用户>`,派工时打 tar 送进 `/work/home`(只读),摘要进 ticket 与 Assignment,与 `aios/home.py` 同一个算法;`GET /home`、`/home/file` 读写删单个文件(人改立刻生效) |
| `intake/config.py` | 环境配置、常量、正则、`now` / `new_id` |
| `intake/store.py` | SQLite 表、JSON 列、迁移 |
| `intake/policy.py` | 默认策略、Policy(Gate 规则、版本哈希) |
| `intake/clients.py` | 控制面与模型代理的客户端、InfraError |
| `intake/gitea.py` | 代码托管 Connector 的方法集与 Gitea 实现:读端(Source 轮询)与写端(评论、PR、合并、文件、归档) |
| `intake/github.py` | GitHub 实现:同一方法集,REST v3 |
| `intake/connectors.py` | 按 CODE_HOST 选 Connector |
| `intake/chat.py` | 聊天工具 Connector:webhook 进(飞书 / Slack / 通用,共享密钥)、webhook 出 |
| `intake/sources.py` | 系统触发、工作台触发、CI / 定时 Source |
| `intake/router.py` | 收消息:去重、归属、合并、停止、退回、例行草稿;人的动作(停止、重试、回答) |
| `intake/gate.py` | Eval:Gate、Assessment、Decision、accept、重验证 |
| `intake/executor.py` | 派工:沙箱、准备、运行、结果、Report、看门狗 |
| `intake/relay.py` | 对话与 say、outbox、批准与中继执行、PR 跟随 |
| `intake/memory.py` | 记忆四层与两个时刻 |
| `intake/agentstate.py` | 例行、agent 的版本、更新与回退 |
| `intake/views.py` | 阶段、工单视图、待处理、会话、实时 |
| `intake/mining.py` | 从合并的 PR 挖任务:分类改动文件、取归档与内容、交给评测器;被纠正过的工单挖成 dependent 题 |
| `intake/commands.py` | 对话里关于它自己的话:改预算、"你接了哪些/状态"的清单 |
| `intake/rulecheck.py` | 规则的检查:三种确定的检查(路径禁止、新文件命名、新增行禁止)的校验、从补丁取事实、跑检查 |
| `intake/intent.py` | 第二双眼睛:长任务在固定步数处由宿主读一遍,缺的是只有人能说的东西时让 agent 去问 |
| `intake/review.py` | 不可逆动作的独立审查:合并(CI 路径、密钥、删测试、测量代码、体积)、向外发、预算、例行,不可信来源与自发工单加严;后台模型审查只升不降 |
| `intake/handoff.py` | 交接:检查、在接收者的 Engine 里开单、结束交出者的单、把接收者的话镜像回来 |
| `intake/corrections.py` | 纠正:认出(插话、否决、退回、停止)、提炼成规则写进记忆、对话里"记住了"、给挖任务用 |
| `intake/httpapi.py` | 路由表与 HTTP 服务(不叫 http,免得盖住标准库) |
| `intake/engine.py` | Engine = 以上 mixin 的组合,加 pass 循环与恢复 |
| `intake/engines.py` | 每个用户一个 Engine(共用沙箱、模型代理、代码托管、评测器),按 X-User 取 |

## 开放问题

Trigger 字段的信任边界(接单层);控制面多实例时路由表的共享存储;jail 下的 uffd(需要自写带 ambient capability 的启动器)。
