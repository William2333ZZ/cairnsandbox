# 012 扩展手册

本文给出几种常见扩展的步骤。它使用 [002](002-domain.md) 的术语与 [011](011-architecture.md) 的模块。

## 加一个层配方

1. 建 `recipes/<name>/recipe.sh`:以 root 在构建沙箱里运行;apt 前先 `mkdir -p /var/cache/apt/archives/partial /var/log/apt`;源用 http;结束前 `apt clean`、删 lists,减小层体积。
2. 附带文件放 `recipes/<name>/files/`(进沙箱 `/tmp/recipe-files`);大二进制不进仓库,在 Mac 下载后 scp。
3. `node/build-layer.sh <name> debian-13 [父层 id]`;层 id 写到 `state/<name>-layer-id.debian-13`。
4. 层是制品:配方或文件改了必须重建。
5. 在 `node/test-template.py` 或专门脚本里加一条自检;CI 的"配方解析"步骤会检查 `recipe.sh` 语法。

## 加一个任务集

1. 从用户项目挖:选一个有测试覆盖的功能,挖空实现(保留签名与 docstring)作为 `init/`,测试放 `verify/`,原实现放 `reference/`。
2. 写 `meta.json`(id、domain、group、timeout_s、allowed_write、smoke)与 `prompt.md`。
3. `verify/verify.sh` 的最后一行输出 `SCORE <0..1>`;带变体输入,防硬编码。
4. 确认 `init/` 没有 `.git`、测试、答案;确认 `.gitignore` 的模式都锚定到仓库根,否则参考文件进不了克隆。
5. 任务集需要的工具链进 env 层配方,不写进任务。
6. `POST /tasksets/<name>/check` 跑导入检查;invalid 的任务修到通过。
7. 先跑种子基线看通过率分布;全 0 全 1 的任务标无区分力。

## 加一个 ABI 操作

1. 在 `abi.rs` 加请求与响应结构、处理分支;检查调用方 uid 是否允许;所有写入都进事件日志。
2. ABI 版本号加一;`hello` 的能力表加该操作名。
3. `aios/abi.py` 加客户端方法;socket 不存在时返回空值。
4. harness 层声明所需 ABI 版本。
5. `test-template.py` 加一条检查;重建 runtime、基础镜像,重推仓库,重启节点。
6. 这是 R3,进人工评审。

## 加一个 harness 语言

1. 解释器进 `recipes/agent-langs/recipe.sh`,装到 `/aios/runtime/<lang>`,软链到 `/aios/bin/`。
2. harness 代码进 `recipes/agent-harness/files/harness/<lang>/`,启动器 `/aios/bin/aios-<lang>` 设好路径与环境,遵守 [004](004-guest.md) 的最小义务与 CONTRACT §5 的退出码与轨迹格式。
3. 文本型组件与 Python 种子共用;只有中间件按语言实现。
4. 评测器登记一个版本,`agent_cmd` 指向新启动器,与 Python 种子在同一任务集上配对比。

## 加一个 Source(接单层)

1. 先定映射:Trigger、Thread、发送者、Channel 地址、接收范围各对应外部系统的什么;这些 id 会被持久化,之后改会断开记录。
2. 实现 kind:`kind`、`title`、配置 schema 与默认值、健康项、`scope`(只依赖元数据与配置,范围外的存为 filtered)、`channel.send`(以消息 id 幂等;永久失败返回 gone,其余抛错由 Outbox 重试)、`policy`(白名单、准入表行、bot 名单、如何点名)、`environment`(进 Ticket 的变量,不含凭证)。
3. 翻译事件:用外部 id 派生 Trigger id;设 `actor`、`human`、`addressed`;agent 自己的消息不产生 Trigger;文本与元数据当不可信数据。
4. 轮询循环:等就绪、Source 关闭时暂停、每轮读配置、游标只在全部 Trigger 存下后前进、畸形项计数跳过、连续三次失败才报健康错误。
5. 注册到服务,默认关闭;测试覆盖翻译、轮询、发送结果、Policy 行、生命周期。

## 加一个 Watchdog 信号

1. 定义只依赖事件流的判定;写进 [006](006-execution.md) 的表。
2. 第一版只报警:面板告警、作弊信号计数;不干预沙箱。
3. 用录下来的事件日志做单元测试。

## 加一种纠正的入口

1. 定义"用户在这里把 agent 扳回来"是什么动作,是否带话(带话的才会被提炼成规则,只有信号的只计数)。
2. 在发生它的地方调 `note_correction(ticket, kind, text, context)`:kind 取现有四种或在 [005](005-intake.md) 的表里加一行;`context` 放当时 agent 做的事(提炼规则要用)。不要在 harness 里认纠正——认的是宿主。
3. 若这个动作自己的处理里也会 `answer_ticket`(比如批准的结果),传 `learn=False`,免得同一句话被认两次。
4. 测试:这个入口产生一条 correction、有话的得到规则、没话的不得到;一条回答 agent 提问的话不算纠正。

## 加一种规则检查

1. 只加**确定**的检查:输入是补丁的事实(`patch_facts` 给出路径、新文件、新增行),输出是违反与否;模型写的只能是参数(一个正则),不能是代码。
2. 在 `rulecheck.py` 的 `KINDS`、`compile_check`(校验参数,太宽的丢)、`violations`(判定)里各加一处;提炼提示(`corrections.py` 的 `_distill`)里加一行用法与例子。
3. 测试:合法与非法的参数、真补丁上的违反与不违反;再确认完成关(`rule_gate`)与合并审查都会用到它。

## 加一条审查规则

1. 写下它在防什么:一个被注入或越界的 agent 想做的事里,哪一种会通过这条检查?
2. 在 `review.py` 对应动作的函数里加 `flag("warn"|"block", 一句话)`:block 只给"几乎不会有正当理由"的情形(密钥、CI 路径、没写数目的预算),其余 warn;原因写给用户看,要具体到文件名或数目。
3. 测试里让一个正常的草稿通不过之前先让它报 `ok`:新规则不能把常态变成噪声。

## 加一种任务

1. 先问它回答的是哪一件事(007 的四种:对不对、记不记得、问不问、做到哪)。新问题再加一种,不要把它塞进已有的。
2. 导入时写进 `meta`(`kind`、`rules`、`blockers`);运行时的行为在 `Runner`(提示怎么拼、要不要多轮),打分在验收脚本(`SUBTASK` 行给部分分),摘要在 `_kinds_and_asks`。
3. harness 看到的措辞若和真实工单里的相同,放在 `aios/run.py` 里一处,评测器镜像一份,单元测试钉住两者一致。
4. 挖这种任务的来源写在 `intake/mining.py`(来自真实的事)或 `eval/mine_history.py`(来自历史,标 origin);合成的要标明合成,并有"答案在改动里、请求没把它泄露"的检查。

## 让一个 agent 能被交接

1. 在用户的 agent 列表里加一个(名字加岗位,`POST /agents`);岗位文本会作为信息进它的每张工单。
2. 不用改代码:别的 agent 的 ticket 里会看到这个名字,说"交给 名字"的请求由 Router 直接送去。
3. 若要改接缝(链长、谁拥有),改 [002](002-domain.md) 的 Handoff 一行再改 `handoff.py`,并更新 `test_corrections.py` 里交接的三个测试。

## 把一个对 harness 的改动送过评测

1. 改仓库里的 `aios/`,单元测试过(`tests/unit/test_aios_seed.py` 等)。
2. 把整个 `aios/` 目录、一份写清"改了什么、为什么"的 `CHANGES.md`、一份 `MANIFEST.json`(预计修好哪些任务、可能弄坏哪些,没把握就写空)放到开发机上一个目录。
3. `docker cp` 进评测容器后:`evolve.py candidate --taskset <集> --parent <父代版本> --dir <目录> --changes <CHANGES.md> --manifest <MANIFEST.json> [--base-batch <已经跑过的基线批次>]`。它走与元 agent 的提案同一条路:critic 读 diff、建层、登记版本、比较、同一条接受规则(含效率一臂与稳定通过的护栏)、清单与结果对照、被接受的以更新送到 agent 面前。
4. 看 `evolve.py explain`:预计与实际差多少、什么变差了。不要因为"是我自己写的"就跳过这一步。

## 沙箱云:加一个节点

1. 在新机器上起 `fc-node`(镜像、`/dev/kvm` 等设备与能力见 [deploy/README](../deploy/README.md) 或 `run-node.sh`),`LAZY_STORE` / `STORE` 指向控制面那边的 fc-store。
2. 把它的 `node-token` 放到控制面能读到的路径(同机:`DATA_DIR/<状态目录>/node-token`;跨机:复制过来)。
3. `POST /nodes {name, url, token_file, reserve_vms?, reserve_mem_mib?}`(admin)。登记写进共享注册表,其他副本下一次轮询就认识它;`GET /nodes` 看到 `alive`。
4. 模板与层不会自动出现:`POST /templates/everywhere` 与 `POST /layers/<id>/everywhere` 把常用的分发过去,控制台"模板与构建"的分发矩阵看是否补齐。
5. 要给人留余量的节点,登记时带 `reserve_*`;要先不接新活就 `POST /nodes/<n>/cordon`。

## 沙箱云:加一种计量用量

1. `cp/internal/metering/metering.go`:加一个 `Kind…` 常量与它的单位;`DefaultPrices` 里给默认价(不给就是 0,账单里有量没钱)。
2. 采集:节点要报这个数(`node/nodeagent.py` 的 `info()` 加字段),`Tick` 里按增量读它、写 `usage_events`(参考 `egress_bytes` 的写法:用 `meter_egress` 记上次读数,第一次只设基线)。
3. 价目表是版本化的:`PUT /metering/prices` 合并成新版本,旧账不变;结算账本冻结的账单不受后来价格影响。
4. `cp/internal/httpapi/server_test.go` 里的计量测试加一条断言;`docs/guide/api/metering.md` 与 CONTRACT §1 的计量一节加这一种。

## 沙箱云:加一种出口规则条件

1. `node/egress_proxy.py`:`validate_rules` 认这个键、`decide` 里按它匹配;只有明文 HTTP 才看得到的条件(path、method)要像现有的那样限定 `scheme == "http"`。
2. `cp/internal/network/policy.go` 的 `Validate` 同步认这个键(控制面先校验,错的不会到节点);`requests.go` 的 `Relaxes` 要把"这个条件被去掉或放宽"算成放宽(否则成员能绕过审批)。
3. 单测:`tests/unit/test_egress_policy.py`(匹配)、`cp/internal/network/requests_test.go`(Relaxes 表)各加一行;真机 `node/test-egress.py` 加一步。
4. CONTRACT §1 的规则语法一行与 `docs/guide/api/network.md` 更新。

## 沙箱云:加一个 MCP 工具

1. `sdk/python/cairnbox/mcp.py`:`TOOLS` 加一项(名字、描述、`inputSchema`),`Server.call` 里加分支,调 SDK 的方法(SDK 没有就先在 `sandbox.py` / `client.py` 加)。
2. `tests/unit/test_sdk.py` 的 MCP 测试加一次调用,并在假控制面里补上对应路由。
3. `docs/guide/mcp.md` 的工具表加一行。工具名一律 `sandbox_` 开头,参数用沙箱 id 字符串,返回 JSON 文本。

## 沙箱云:加一个模板来源

1. 来源在节点侧生成一个**层**(`node/nodeagent.py`:参考 `layer_from_oci` / `layer_from_archive`:把内容解到一棵树,过 `_tree_to_layer` 叠在 base 上——base 赢,不要绕开它),并在 `/layers/...` 下开一条路由;大请求体走 `h._body` 的循环读。
2. 控制面:`cp/internal/images/builds.go` 的 `Builds` 加一种 `kind`(Build 记录输入、节点、状态、结果),`httpapi/server.go` 的 `/builds` 路由认它。
3. 真机测试 `node/test-images.py` 加一步(CI 里没有外网,归档类的来源要带自己的小样本)。
4. `docs/guide/api/images.md` 与教程 `custom-template.md` 更新。
