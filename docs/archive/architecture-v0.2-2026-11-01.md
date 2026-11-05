# 云端沙箱管理系统 + 自改进 CairnSandbox:架构 v0.2

版本:v0.2(2026-10-30,取代 v0.1;v0.1 存档在 `archive/architecture-v0.1-2026-10-29.md`)
范围:平台(沙箱管理、控制面、节点)、沙箱内的分层系统、CairnSandbox 及其两个改进循环、评测器、客户端。功能细节见 `feature-design.md`,数据与存储见 `data-and-storage.md`,算法依据见 `rsi-algorithms-survey.md`,与 DSec 的对照见 `dsec-vs-ours.md`,原型实测见 `prototype-firecracker.md`。

---

## 0. 一页摘要

- 产品:一个用户有多个沙箱;每个沙箱是一台 Firecracker microVM,里面跑一个分层的最小 Linux 系统,系统的上层就是 CairnSandbox(agent 的 harness);CairnSandbox 在沙箱外的评测器裁判下自我改进;Web 工作台和桌面端是入口,桌面端可以在本地跑同一套沙箱。
- v0.1 到 v0.2 的变化,来自一天的文献梳理和原型:
  1. **沙箱内的系统分五层**(第 3 节):内核层、系统层、agent-runtime(Rust 二进制:sandboxd + CairnSandbox 内核 ABI + Landlock)、agent-langs(Python 与 TS 解释器同在)+ agent-harness(只有代码,默认 Python,RSI 改的层)、env;每层是配方加内容寻址产物;CairnSandbox 版本 = runtime、langs、harness 三个哈希;稳定的 harness 组件逐步晋升为 runtime 里的 Rust 代码。
  2. **CairnSandbox 的可变层有了确定的形状**(第 4 节):借 Harness² 的 8 个文件组件;运行时(执行循环、REPL、子 agent)和可变状态分开,运行时可以是我们的种子(mini-swe-agent 裁剪版)或 Prime Agent,由评测数据决定。
  3. **改进有两个循环**(第 5 节):任务内循环在工作台里,没有真值,靠对照执行(Harness² 的 Propose/Probe/Compose),用 fork 做探测;版本循环在评测器里,有真值,靠噪声带和配对统计决定晋升。前者产出候选,后者裁决。
  4. **评测器的任务来自用户自己的工作**(第 6 节):从用户项目里挖带验收的任务,公开基准只做回归;第一个用户项目就是这个平台本身。
  5. **语言按层定**(第 12 节):冻结的、安全关键的用 Rust;被循环改的用 Python;"Python 用来演化,Rust 用来冻结"。
  6. 平台层已经达到 DSec 的大部分能力(`dsec-vs-ours.md` 第 0 节),v0.1 里"首发 Docker、microVM 后置"的说法作废:后端就是 Firecracker。

核心原则(修订):

1. **三区分离靠权限,不靠约定**:不可变内核与基础设施(agent 不可写)、可变 harness(agent 可写)、评测器(agent 不可见);边界由用户、只读挂载、Landlock、宿主机侧网络与成本上限强制。
2. **每一层都是配方加产物**:内核、底座、平台、CairnSandbox 内核、CairnSandbox 可变层、工具链、任务环境,全部内容寻址、可回滚;没有"在机器上手动装"的状态。
3. **评测必须回答产品问题**:只测"这个用户的事做得更好了吗";通过率在天花板或地板上的任务不进入比较;提升小于噪声带不算提升。
4. **不信 agent 自报**:成本、网络、审计、分数都在沙箱外记。
5. **能不自研就不自研**:不写 VMM、不写 OS 内核、不写分布式存储、不写 agent 运行时(先用现成种子)。
6. **为规模预留接缝**,目标几百到几千并发(`decisions.md` D6)。

---

## 1. 总体架构

```
┌───────────────────────────────────────────────────────────────┐
│ 客户端                                                          │
│   Web 工作台(原生 JS,控制面直接提供)   桌面端(同一前端 + 本地站点)│
└──────────────┬──────────────────────────────┬─────────────────┘
               │ HTTPS                         │ 同一协议,site=local|cloud
┌──────────────▼──────────────────────────────▼─────────────────┐
│ 控制面(已实现为单进程,无持久运行态,可从节点重建)                  │
│   节点注册与健康 │ 放置(power-of-two + 反馈)│ 路由 │ 转发含流式     │
│   迁移/接管/对账 │ 模板分发 │ 静态工作台 │ /eval 转发               │
│   模型 API 代理(每沙箱令牌、预算、计量)                            │
│   评测服务(任务集、版本、批次、运行;结果矩阵)                      │
│   [后续] 账户与配额 │ 记忆服务 │ 产物库 │ 只追加审计 │ 晋升控制器     │
└──────────────┬────────────────────────────────────────────────┘
               │ HTTP + bearer
┌──────────────▼────────────────────────────────────────────────┐
│ 计算节点(节点代理)                                               │
│   准入 │ TTL │ 快照协调 │ 每 VM tap + nftables + 出口代理 │ 排空     │
│   分层镜像缓存(分块仓库按需加载、页缓存共享)│ 模板快照与预留池       │
│   ┌──────────────────────────────────────────────────────┐    │
│   │ 沙箱 = Firecracker microVM,分层系统(第 3 节)            │    │
│   │  L0 内核+init+libc │ L1 平台 daemon+用户边界 │ L2 CairnSandbox 内核 │    │
│   │  L3 运行时 │ L4 CairnSandbox 可变层(8 组件)│ L5 工具链 │ L6 任务环境  │    │
│   └──────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────┘
┌───────────────────────────────────────────────────────────────┐
│ 存储:分块仓库(镜像层、bundle)│ 评测器存储(独立)│ 审计(独立)      │
│      [后续] 对象存储、元数据库、记忆索引                             │
└───────────────────────────────────────────────────────────────┘
```

---

## 2. 平台层(已实现的部分按实际写)

### 2.1 节点代理

创建、销毁、暂停(Diff 内存快照链)、恢复(透明)、fork(写时复制)、pack(把可写层固化为新层)、模板快照直启与预留池、SIGTERM 排空、每 VM 网络策略、jailer 加固、QoS(SCHED_IDLE、core scheduling)、沙箱规格含 `layers` 与 `disk`。细节与数字在 `prototype-firecracker.md` 第 7 到 15 节。

### 2.2 控制面

单进程,运行态以节点为准、路由持久化、重启后从节点对账重建。放置、转发、迁移(export → import → delete)、死节点接管(从最新 bundle)、模板分发。静态提供工作台,转发评测服务。多实例、账户配额、审计流、晋升控制器是后续项。

### 2.3 存储

分块仓库(内容寻址,HEAD/PUT 去重,bundle 清单带稀疏 extent,GC);节点 FUSE 按需加载加共享缓存加启动画像预取。换 S3 只改取块函数。

### 2.4 网络策略

每个沙箱一条策略,宿主机侧强制,guest 内无 DNS:

| 模式 | 适用 | 规则 |
|---|---|---|
| 关闭 | 评测沙箱默认 | 只许模型代理 |
| 只许缓存 | 评测中装包 | 包管理器只能命中读穿缓存,未命中即失败并记任务失败 |
| 白名单 | 工作台默认 | 模型代理、包缓存、搜索服务、用户指定域名 |
| 开放审计 | 工作台可选 | 全部经代理,逐 URL 审计,带宽与请求配额 |

搜索与抓取是沙箱外的服务,沙箱里只有调用它们的工具。

---

## 3. 沙箱内的分层系统:五层

按"谁拥有、谁版本化"分层;每层是配方加内容寻址产物加父哈希。叠放顺序:内核 → 系统 → agent-runtime → agent-langs → agent-harness → env → 可写层。**CairnSandbox 版本 = agent-runtime 哈希 + agent-langs 层哈希 + agent-harness 层哈希**(前两者很少变,日常只有 harness 层在变)。

| 层 | 内容 | 属主 / 可变性 | 位置 |
|---|---|---|---|
| **内核层** | 裁剪的 Linux 6.18(官方 microvm 配置做减法,补 EROFS;Landlock ABI ≥ 6;关 io_uring、user namespace、模块) | 平台,冻结 | vmlinux |
| **系统层** | Debian essential 底座:libc、coreutils、bash、util-linux、iproute2、curl;三个用户 root / aios / agent;无解释器、无 systemd | 平台,冻结 | `/`、`/usr` |
| **agent-runtime** | 一个 Rust 静态二进制 `sbx-runtime`:sandboxd(vsock 协议)+ CairnSandbox 内核 ABI(unix socket、SCM_CREDENTIALS 认 uid、能力表、本地预算计数、只追加事件日志、检查点钩子、给 harness 进程下放 Landlock/seccomp)+ 从 harness 晋升进来的稳定组件 | 平台与晋升组件,人工评审(R3),冻结 | `/usr/local/lib/sandboxd`,以 `aios` 用户运行 |
| **agent-langs** | harness 可用的解释器,同时装 Python(可重定位 CPython 3.13 + jinja2)和 TypeScript(bun 单文件),在 `/aios/runtime/{python,bun}`,不碰 `/usr`;所有 harness 版本共享 | 平台构建,换解释器版本是 R3 | `/aios/runtime`、`/aios/bin/{python3,bun}` |
| **agent-harness** | harness 代码与 8 个组件(第 4 节),只有代码;**默认 harness 是 Python**(`/aios/bin/aios`),TypeScript harness 走 `/aios/bin/aios-ts`,两者可并存 | **RSI 改的就是这层**:R1 文本、R2 代码 | `/aios/harness/`,以 `agent` 用户运行,只能经 ABI socket 产生副作用 |
| **env** | 任务或项目需要的工具链与依赖(gcc、git、make、pytest、node、系统 python……) | 任务集导入时在构建沙箱里构建,随任务集版本固定 | `/usr`、`/opt` |
| (工作区) | 可写层,`disk` 规格决定大小 | agent | overlay 上层 |

### 3.1 harness 向 runtime 的晋升路径("Python 用来演化,Rust 用来冻结")

1. harness 里的某个组件在档案库里连续若干版本不再变化、评测稳定,成为冻结候选。
2. 人把它重写成 Rust,放进 agent-runtime,经 ABI 暴露为一个能力;harness 改为调用该能力。
3. 新 runtime 层 + 去掉该组件的 harness 层作为新 CairnSandbox 版本走评测,不退步才晋升。
4. 长期 agent-runtime 变厚、agent-harness 变薄,最终 CairnSandbox 接近"一个 Rust 二进制 + 一组文本组件"。

硬规则:晋升进 runtime 的东西从此只能人改;ABI 版本化,harness 层声明所需 ABI 版本,不兼容的 runtime 层不能叠。

### 3.2 规则

- 隔离是文件系统和内核事实:runtime 以 `aios` 用户运行、代码只读;harness 以 `agent` 用户运行,Landlock 作用域(6.12+)把"只能和内核 socket 说话"强制住。
- 解释器在 agent-langs 层,Python 与 TS 同时在,默认 harness 用 Python;harness 层只放代码,所以很小,RSI 每个候选重建的也只是它。
- 不用 unikernel:没有多用户与完整 fork,做不了 guest 内特权分离。
- 回滚 = 换回旧的层哈希组合;升级 = 新层加编号迁移脚本,迁移脚本是候选改动的一部分,要进评测。
- 每次构建后自检:能编辑代码、能调模型、能写审计;不过的层不进评测。
- 原型状态(2026-10-30 晚):五层都已落地。内核层、系统层、agent-runtime(sandboxd + 内核 ABI v1 + Landlock 下放)在模板 `debian-13`;`agent-langs`(python + bun)、`agent-harness`(Python 种子,`/aios/bin/aios`)、`dev-tools`(env)三个层由配方构建;评测器默认层 = langs + harness + dev-tools,版本记录 runtime 与 harness 哈希。TS harness 还没有,`aios-ts` 启动器是占位。

---

## 4. CairnSandbox:运行时与可变层

### 4.1 可变层的 8 个组件(借 Harness²)

| 组件 | 内容 | 类型 |
|---|---|---|
| 系统提示 | 基础行为指令 | 文本 |
| 护栏 | 禁止项与确认项 | 文本 |
| 记忆 | 事实、项目知识 | 文本 |
| 技能 | 按需加载的过程,旁边放脚本 | 文本 + 代码 |
| 工具指引 | 每个工具怎么用、何时用 | 文本 |
| 脚本 | 可执行的辅助程序 | 代码 |
| 子 agent | 可复用的角色规格 | 文本 |
| 中间件插件 | 工具循环内的钩子(重试、压缩、路由) | 代码 |

每个组件是一个文件或目录,可以单独 diff、审、回滚。R1 只改文本型组件,R2 开放代码型组件,R3(改运行时或 ABI)永远人工。

**语言无关约束**:文本型组件与运行时语言无关;脚本经 bash 调用,任何语言都行;只有中间件插件绑定运行时的语言。这样 L3 从一个运行时换到另一个(Python 种子、TS 的 pi、Prime Agent),可变层大部分不动。

### 4.2 运行时:Rust 基础运行时在平台域,harness 的语言由 CairnSandbox 版本自己定

边界就是 D4 要求的权限边界;harness 用 Python 还是 TS 不再是平台决定,而是每个 CairnSandbox 版本的 runtime 层决定(第 3 节):

| | agent-runtime(`sbx-runtime`,Rust) | agent-harness(Python 或 TS,自带解释器) |
|---|---|---|
| 身份 | `aios` 用户,代码只读 | `agent` 用户,可写 |
| 职责 | sandboxd(exec、PTY 会话、文件、pack、校时、网络配置、冻结)、系统调用 ABI、能力检查、本地预算计数、只追加事件日志、检查点钩子、进程组与超时、给 harness 进程下放 Landlock/seccomp、模型网关客户端 | agent 循环、提示、工具语义(read/write/edit/bash 怎么用)、技能、记忆、子 agent 编排、中间件插件,即第 4.1 节的 8 个组件全部 |
| 可变性 | 冻结,人写,人工评审(R3) | RSI 改的全部在这里(R1 文本,R2 代码) |
| 接口 | unix socket 上的 NDJSON RPC,SCM_CREDENTIALS 认调用方,ABI 版本化 | 只能通过这条 socket 产生副作用 |

一句话:Rust 提供"能做什么"(带超时、配额、审计的执行原语),harness 决定"怎么做"(策略)。Landlock 作用域把"harness 进程只能和内核 socket 说话"强制住;元 agent 改 TS 时碰不到任何执行原语。这是 Prime Agent 验证过的形状(Rust 守护进程 + 脚本语言层),边界画在我们的位置上。

第一个 CairnSandbox 版本就是现在的 Python 种子:runtime 层 = 可重定位的 Python(python-build-standalone,约 30 MB),harness 层 = `aios/`。TS 种子(以 pi 的 agent-core 为参照)是另一个版本:runtime 层 = bun 单文件(约 90 MB)。两者在同一任务集上配对比,评测器不用变。

已落地:sandboxd 用 Rust 重写并替换 Python 版,底座去 python3(冷启动 1.0 → 0.4 s)。下一步:同一二进制加 ABI 层;拆出 `aios-runtime` 与 `aios` 两层;TS 种子。

---

## 5. 两个改进循环

```
工作台(任务内,无真值)                       评测器(版本间,有真值)
┌─────────────────────────────┐            ┌─────────────────────────────┐
│ 用户任务 q                    │            │ 任务集(用户项目挖出,带验收)   │
│ 参考执行 ─┐                   │  候选      │ 种子基线 k 次 → 噪声带 δ       │
│ Propose 两个编辑 ─ fork 探测 ─┼──────────▶│ 级联:烟测 → 核心集 → 全集+留出│
│ Compose → 后继 harness → 交付 │  改进库    │ 配对差、bootstrap CI、McNemar │
│ 反思 → 改进库(EXPERIENCE)     │◀──────────│ 接受门 → 入档/淘汰 → 晋升     │
└─────────────────────────────┘  已晋升版本  └─────────────────────────────┘
```

### 5.1 任务内循环(Harness² 的 Propose / Probe / Compose)

- 用户在工作台跑一个任务,先用当前 harness 做参考执行。
- 提两个候选编辑:一个跨领域的"不变量"修正,一个领域特定修正。
- **探测用 fork**:从参考执行的某个检查点 fork 出候选沙箱,匹配条件继续跑;这是我们相对 Harness²(每个候选重开容器)的平台优势。
- Compose 决定采纳、合并或拒绝;选择器只看匿名化的交付物,不知道哪个 harness 产出的。
- 任务结束反思,把有执行对照支撑的经验写进项目级改进库(可执行 harness + EXPERIENCE.md)。
- 这里不打分。评测分数、rubric、验收脚本对编辑器和选择器永远不可见。

### 5.2 版本循环(评测器)

- 候选来源:改进库里被采纳的 harness、元 agent 在 dev 沙箱里针对失败轨迹提的 diff。
- 流程:构建候选层 → 自检 → 烟测(3 到 5 题,失败即淘汰,余题计 0)→ 核心集(有区分力的任务,每题 k 次)→ 位列前二才跑全集与留出集。
- 接受:配对提升 > δ 且 bootstrap 95% CI 下界 > 0 且留出集不退步且成本增量满足 ΔC ≤ β0 + β1·ΔS 且无作弊告警;噪声带内的候选不晋升,按成本与新颖性入档。
- 档案库是树,每节点保留结果矩阵与 Beta 后验;父代按子树成功率的 Thompson 采样 × 1/(1 + 子代数)抽取;新颖性过滤;稳定版终选用后验保守分位。
- diff 级 critic 在评测前跑:含任务名、答案、任务特定分支的编辑拒绝;触及评测、记录、传播相关代码的改动按篡改分类。
- 匹配预算的并行采样基线:harness 提升必须也胜过"同样的钱直接多采样"。
- 晋升由沙箱外不含 LLM 的晋升控制器执行,只接受评测器签名的结果(`decisions.md` D3)。

---

## 6. 评测器与任务来源

### 6.1 已实现

评测服务:任务集、版本、批次(基线 / 比较 / 导入检查)、运行;每次运行 = agent 沙箱 → 导出工作区 → 销毁 → 独立无网络验证沙箱跑验收;基础设施失败重试三次并单独计数;avg@k、pass@1、pass^k、半分法 δ、配对差 + bootstrap CI + McNemar、级联、作弊信号、导入检查(参考解过、空解不过、重复一致、时长)。模型代理在沙箱外按沙箱令牌计量。工作台展示批次矩阵与轨迹。

### 6.2 任务从哪来(修订)

| 来源 | 真值 | 用途 |
|---|---|---|
| 用户项目里挖出的任务:把一个有测试覆盖的功能挖空(保留签名与 docstring),藏起测试做验收;或从带测试的提交还原 | 有 | **演化集、验证集、留出集** |
| 工作台任务流(接单层的工单,见 `intake-layer.md`) | 无(用户接受、拒绝、修改量) | 任务内循环与改进库;合并且带测试的 PR 产出评测任务候选 |
| 公开基准(Polyglot、Terminal-Bench 子集) | 有 | 只做回归与外部坐标 |

第一个用户项目是这个平台本身:CairnSandbox 改进的是"把这个平台开发得更好"的能力。自建玩具任务已证明无信息(全满分),停用。

### 6.3 噪声的位置

δ 不是独立实验,是接受准则里的一个数;只有在任务通过率离开天花板和地板之后才有意义。顺序:真实任务集 → 种子通过率分布 → 剔除全 0 全 1 → 再测 δ。几十个任务只能检出大提升,这是事实,不自欺。

---

## 7. 数据模型(修订)

| 实体 | 关键字段 |
|---|---|
| Layer | id(内容哈希)、名称、配方、父层、大小、构建沙箱审计 |
| Version(CairnSandbox 版本) | id、parent_id、层哈希列表、L4 组件哈希、运行时标识、agent 命令、状态(候选/稳定/淘汰)、Beta 后验、来源(改进库 / 元 agent / 人工) |
| TaskSet / Task | 版本号;任务:id、领域、来源标签、分组、时限、允许写路径、烟测标记、历史通过率、区分力状态;验收脚本与隐藏测试在评测器存储 |
| Batch | 类型(基线 / 比较 / 检查)、任务集版本、版本、k、级联、进度、统计、接受决定与理由 |
| Run | 任务 × 版本 × 第 k 次;分数、基础设施失败、重试次数、token 与费用、时长、作弊信号、轨迹、沙箱与节点 |
| ImprovementLibraryEntry | 项目、可执行 harness 引用、EXPERIENCE 文本、支撑它的执行对照、采纳次数 |
| Sandbox | 同 v0.1,加 `layers`、`disk`、`network.mode`、`boot_mode` |
| 其余(User、Project、Snapshot、Checkpoint、Event、MemoryItem、Artifact、Experiment、Approval、AuditRecord) | 同 v0.1 |

---

## 8. 安全模型(修订)

| 威胁 | 对策 |
|---|---|
| 沙箱逃逸 | Firecracker + jailer;guest 内核裁剪(无模块、无 io_uring、无 user namespace) |
| 可变层篡改内核 | L2 只读、独立用户、socket ABI、Landlock 作用域(6.12+)、no_new_privs、seccomp |
| 窃取或篡改评测 | 验收在独立沙箱;测试事后拷入或只读;镜像无 .git 历史、答案载体;失败只回传异常类型;评测器不依赖被评代码的日志 |
| 作弊固化 | `/refine`、改进库的采纳不等于晋升;逐 run 作弊信号;分数高于历史 2σ 送人工;不可能任务探针;篡改分类 |
| 成本与网络 | 代理计量与预算在沙箱外;guest 无 DNS;策略模式宿主机侧强制 |
| 投毒的基准 | 任务集来源签名、版本化 |
| 其余(无限输出、凭证、提示注入、权限升级审批) | 同 v0.1 |

---

## 9. 客户端

工作台已实现:沙箱列表与创建、详情(流式执行、PTY 终端、文件、网络、暂停/恢复/fork/打包)、节点、实验(任务集、版本、批次)、批次矩阵、运行轨迹。后续:任务内循环的界面(参考执行、两个候选、对照、采纳)、改进库浏览、版本树、审批卡片、桌面端与本地站点(`decisions.md` D2)。

---

## 10. 沙箱 Daemon 协议

同 v0.1 第 5 节(exec、PTY 会话、文件、健康、事件流、幂等、输出上限),已实现;后续加:L2 内核的审计事件进入同一事件流;会话与进程组的资源账目。

---

## 11. 规模与接缝

同 v0.1 第 9 节;已落地的接缝:路由持久化、运行态以节点为准、临时型沙箱不同步、模板快照与预留池、按需镜像加载、放置反馈与节点准入。未做:多实例控制面、事件流水线、真机多节点。

---

## 12. 技术选型 v0.2

| 部分 | 选择 | 理由 |
|---|---|---|
| 隔离 | Firecracker,guest 内核 6.18 裁剪 | 已验证;Landlock 作用域需要 ≥ 6.12 |
| guest 底座 | Debian essential(glibc),去 systemd/文档/locale | Python wheel 需要 glibc;上层要 apt |
| agent-runtime | 一个 Rust 静态二进制 | 冻结、小、内存安全;系统层不需要任何解释器 |
| agent-harness | 每个版本自带解释器:Python(现在的种子)或 TS/bun(pi 式种子) | 语言是 CairnSandbox 版本的属性,不是平台的;稳定组件晋升为 Rust |
| 控制面、节点代理、评测器、代理 | Python 原型,单进程 | 已验证;到多实例与规模前不换语言 |
| 工作台 | 原生 JS,无构建 | 服务器无外网,无依赖 |
| 存储 | 分块仓库;后续 S3、元数据库、记忆索引 | `data-and-storage.md` |
| 模型 | 经代理;当前 DeepSeek V4.1 Flash;Claude 经 OpenRouter | 服务器可达性决定 |

"Python 用来演化,Rust 用来冻结":可变层里多轮不再变化、要晋升进内核边界的组件,重写成 Rust 并走人工评审。

---

## 13. 明确不做

自研 VMM/OS/文件系统/分布式存储/agent 运行时;unikernel;桌面 GUI 沙箱与 GPU;自动修改 TCB、ABI、评测器;用公开基准做优化目标;在共享开发机上做压测。

---

## 14. v0.1 到 v0.2 的差异

| 项 | v0.1 | v0.2 |
|---|---|---|
| 隔离后端 | 首发 Docker,microVM 后置 | Firecracker,已达 DSec 大部分能力 |
| 沙箱内系统 | 一个镜像 + 三区 | 七层配方,语言按层,内核裁剪 |
| CairnSandbox 可变层 | 五个模糊的"服务" | 8 个文件组件(Harness²) |
| 运行时 | "参考 DGM/SICA 种子" | 种子已落地;Prime Agent 为候选,评测定 |
| 改进循环 | 一个循环 | 任务内(无真值,fork 探测)+ 版本间(评测器) |
| 父代选择 | 得分 + 新颖度 | 子树成功率的 Thompson 采样,保守终选 |
| 噪声 | "先测方差" | 噪声带 δ 进接受门;只在有区分力的任务上测 |
| 任务来源 | 未定 | 用户项目挖出;公开基准只回归;第一个项目是平台本身 |
| 控制面 | Go + PostgreSQL | Python 单进程原型,多实例后置 |
| 网络 | 默认断网 + 白名单 | 四种策略模式,工作台与评测分开 |
