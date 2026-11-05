# 接单层设计(Intake):把用户自己的工作变成沙箱里的工单

日期:2026-11-01。状态:设计稿,未实现。前置:`architecture.md` v0.2 §5(两个循环)、§6.2(任务来源)、`decisions.md` D8;对照材料 `dsh-superagent-notes.md`。

## 0. 一页摘要

接单层是宿主侧的一层,位于控制面之上、工作台之下。它监听用户自己工作里的事件(Gitea 的 issue、PR 评论、CI 结果;工作台里的直接请求),判定要不要开工,开工就在一台 Firecracker 沙箱里按某个 CairnSandbox 版本执行,结果回到事件发生的地方(PR、评论)。它解决 D8 留下的空:任务要来自用户自己的工作,但目前任务只能由人手工建任务集。

与 DeepSeek Super-agent 原型的差别有四条,其余领域模型基本照用:

1. **执行单位是沙箱而不是目录**。一次派工 = 创建一台沙箱(模板 + CairnSandbox 版本的层 + 开发工具层),工单的工作区在派工之间以打包层或快照保留。
2. **凭证不进沙箱**。回帖、推送分支都由宿主侧的中继用 agent 账号的令牌完成;沙箱只产出补丁或分支快照。
3. **工单状态经内核 ABI 自报**。harness 向 agent-runtime 发 `report`,宿主从事件流读;同一条流也喂 Watchdog。
4. **工单是两个改进循环的进料口**。用户对交付的接受/拒绝/改动量是任务内循环的信号;合并且带测试的 PR 是评测任务的挖掘候选。

默认接受模式是 observe:模型判定"该接"的主动工作只记录不执行,先看记录对不对。

## 1. 它在哪一层

```
  Source(Gitea / 工作台 / 以后:飞书、邮件)
     │ Trigger
     ▼
  接单层 fc-intake ── Gate(规则) → Assessment(模型) → Decision
     │ Ticket + Mandate(授权、预算、网络模式、CairnSandbox 版本)
     ▼ Assignment
  控制面 fc-cp ── POST /sandboxes(layers = 版本的层)→ exec /aios/bin/aios run
     │                                      ▲ ABI: report / input / events
     ▼                                      │
  沙箱(五层 CairnSandbox)  ───── 产出:补丁 / 分支快照 / 文本 ──▶ 宿主中继 → Gitea
```

接单层不做技术决策,不改 harness,不打分。它只管"谁授权了什么、派给哪台沙箱、现在等谁"。

## 2. 领域模型

| 实体 | 定义 | 关键字段 | 对 DSH 原型的改动 |
|---|---|---|---|
| Source | 一种事件流的实现及其一份配置 | kind、config、读位置、bot 身份、开关 | v1 两种 kind:`gitea`、`workbench` |
| Trigger | 一个不可变事件 | id(来源稿定,去重)、actor、human、addressed、text、url、threads[]、channel 地址、meta(不可信数据) | 同 |
| Thread | Source 定义的事件分组,按时间排 | id(`gitea:<repo>#<issue>`、`workbench:<conv>`)、订阅集、运行中的 Eval、Decision 历史 | 同 |
| Eval | 无状态判定:Gate + Assessment | 输入:Trigger 批、Thread 快照、Thread 上全部 Ticket、当前 Policy;输出 Decision | Assessment 走模型代理,带自己的沙箱令牌与预算,成本计入接单层 |
| Decision | accept / stop / ignore / unavailable | reason、matched_rules、policy_version、by(gate/model/person) | 同 |
| Mandate | 一次 accept 授予了什么,不可变 | policy_version、scope、strength(explicit/inferred)、matched_rules、sender identity、**budget_usd、network_mode、cairn_version** | 加了预算、网络模式、版本三项:它们是"授权"的一部分,不是执行细节 |
| Ticket | 一个授权的工作单元 | id、短名、mandate、channels[]、inputs[]、state(open/ended)、ended_by(executor/person/pr-closed)、**workspace_layer**(最近一次派工打包的可写层) | 加工作区层 |
| Assignment | Ticket 与一台沙箱的绑定 | id、ticket、**sandbox_id、node、version、layers、boot_mode**、state(pending/running/ended)、最后 Report、重试次数 | 绑定对象从会话改成沙箱 |
| Report | 执行器自报 | progress / waiting / ready / blocked / failed / dropped,+ 结构化 `attention`(问题文本、选项)和 `deliverable`(分支名、补丁哈希) | 经 ABI 上报而不是工具调用 |
| Channel | Thread 的写端 | 地址、kind、最后一次送达 | 送达由宿主中继做 |
| Policy | 版本化的接单规则 | 规则文本列表(每条有名字)、接受模式、各 Source 的白名单与准入表、bot 名单;version = 内容哈希 | 同 |

不变量照搬:只有 Eval 能创建 Ticket;Mandate 只有 accept 创建、人 stop 收回、重验证失败吊销三种变化;扩大范围开新 Ticket;Ticket 自己定义状态,不镜像沙箱状态;身份不跨 Source 映射;消息内容只是数据,不能改变权限。

## 3. 授权

**白名单与准入表**(每个 Source 一份):

| 行(接收范围) | 名单内的人,未点名 | 名单外的人,点名 | 名单外的人,未点名 |
|---|---|---|---|
| gitea:issue 评论 | inferred | ignore | ignore |
| gitea:PR 评论 | inferred | ignore | ignore |
| gitea:新 issue / 新 PR | inferred | ignore | ignore |
| gitea:CI 结果(默认分支) | — | — | 只作上下文 |
| workbench:直接请求 | explicit | — | — |

名单内的人点名 agent(`@cairn` 或工作台直接发)永远是 explicit。bot(`fc-runner`、`[bot]` 后缀、Gitea Actions)只能产生上下文,不能开工、不能 stop。

**接受模式**:`off`(不产生 inferred 候选)/ `observe`(默认;Assessment 判"接"也只记 `would_accept`)/ `auto`(接受 inferred 候选,且必须命中至少一条规则)。explicit 请求在三种模式下都执行。

**规则**是团队写的自然语言,每条有名字,例如 `reproduce-and-fix-simple`:"issue 里报了可复现的 bug,且修法不超过 200 行、不改接口,就提 PR;否则不出声"。Assessment 对每条启用的规则判一次,Mandate 记下命中的规则名,执行器收到规则正文。

**策略版本**变了:丢弃运行中的 Eval、重排其 Trigger;给受影响的在途 Ticket 发一条 `policy_changed` 输入;不立刻吊销 Mandate。ended 的 Ticket 被人唤醒前先重验证(Source 还在听该 Thread;inferred 的要求仍是 auto 且规则仍启用;explicit 的要求发送者仍在名单)。

## 4. 执行:派工到沙箱

**创建**:`POST /sandboxes {template:"debian-13", layers:[版本的 langs, harness, dev-tools, 工单的 workspace_layer?], network:{mode:"workbench"}, class:"persistent", ttl_idle_s: 1800}`,再 `PUT /budget` 写 Mandate 的预算。版本取 Mandate 里的 `cairn_version`(默认当前稳定版;工作台可指定候选版做试用,这就是任务内循环的入口)。

**准备**:首次派工时宿主把仓库以只读的 bundle 塞进沙箱(`PUT /files`)或从快照层恢复,沙箱里 `git clone` 本地 bundle,切到 `ticket/<短名>` 分支。沙箱**没有** Gitea 令牌,也连不到 Gitea。

**运行**:`POST /exec {cmd:"/aios/bin/aios run --ticket <id>", user:"agent", landlock:true}`,流式。工单的原始 Trigger、规则正文、Thread 快照作为 `/work/ticket.json` 送入。

**自报**:harness 通过 ABI 新操作 `report {state, text?, attention?, deliverable?}` 上报,agent-runtime 写入事件流,接单层从 `GET /sandboxes/<id>/events` 读。沙箱退出但没有 `ready|blocked|failed|dropped` 的 Report,视为 `failed`(exit 非 0)或 `ready`(exit 0 且有 deliverable)。

**交付**:`ready` 带 deliverable 时,宿主 `GET /files` 取出 `git bundle` 或 `format-patch`,由中继用 agent 账号推到 Gitea 的 `ticket/<短名>` 分支并开 PR;PR 描述首行固定声明"由自动 agent 产出,非人工"。回帖同样经中继。

**后续输入**:Thread 上的新 Trigger(人评论、CI 结果)扇出到订阅的 Ticket:写进沙箱的 `/run/aios/inbox.jsonl`(ABI 新操作 `input`,只 root 可写,agent 可读),harness 轮询;沙箱已回收则新开一次 Assignment,从 workspace_layer 恢复,附"恢复提示":先看目录、git 记录、PR 状态,再做外部动作。

**并发**:每节点容量由控制面管;接单层自己加一个预留槽给"人的后续消息",它优先于新派工。单次派工时限默认 30 分钟,到时 Report 记 `waiting` 并打包工作区。

**结束**:ready 且无 PR → 结束;PR 合并或关闭 → 结束;dropped → 结束但可被人唤醒;人 stop → 结束且不再唤醒。结束时打包可写层为 `workspace_layer`,销毁沙箱。

## 5. Watchdog

宿主侧独立进程,只读 ABI 事件流,v1 只报警不干预:

| 信号 | 判定 | 动作 |
|---|---|---|
| 停滞 | running 态下 10 分钟无 `model_call`、无 exec | 面板告警,Report 记 `blocked(stall)` |
| 循环 | 最近 20 次命令里重复率 > 60% | 告警 |
| 预算 | `budget_charge` 触顶 | runtime 已拒绝调用;接单层记 `failed(budget)` |
| 漂移 | 命令触及 Mandate scope 之外的路径(Landlock 拒绝事件) | 告警,计入作弊信号 |

v2 再谈干预(注入提示、重开派工)。

## 6. 和两个循环的接口

- **任务内循环**:工单是"用户任务 q"的来源。工作台可以对一个工单开"探测派工":从主派工的检查点 fork 两个候选沙箱(不同 harness 编辑),Compose 后只交付一个。用户在 PR 上的接受、拒绝、改动行数写回 `ImprovementLibraryEntry` 的执行对照。
- **版本循环**:PR 合并且 diff 触及有测试覆盖的代码时,接单层产出一条 `task_candidate`(仓库、提交、涉及文件、测试命令),评测器的挖掘脚本据此生成带隐藏测试的任务;人确认后进任务集。这样任务集随用户的工作自然增长,不再手工编。
- 工单运行的 ABI 事件和轨迹保存格式与评测 Run 一致,工作台同一套视图。

## 7. 服务与数据

- 新服务 `fc-intake`(Python,与 eval 同风格),只绑 127.0.0.1;控制面转发 `/intake/*`。一个 SQLite:`trigger, thread, trigger_thread, decision, mandate, ticket, assignment, report, policy_version, effect, outbox, kv`。
- Effects 持久队列(assessment / launch / deliver / relay / reply),按 key 合并、指数退避、三次失败才算系统问题;Pass 每 2 秒一轮。
- Source `gitea`:轮询 Gitea API(notifications、issue/PR 评论、Actions 运行),读位置存 kv;Source `workbench`:工作台 `POST /intake/requests` 直接产生 addressed Trigger。
- Gitea agent 账号与令牌只存 `./secrets/`,以文件挂入 fc-intake,与现有 DeepSeek key 同一处理方式。

**API(草案)**:
- `GET /intake/attention` → 等人的工单(answer / review / blocked / failed / delivery)优先,再是系统问题
- `GET /intake/tickets?state=`、`GET /intake/tickets/<id>`(mandate、assignments、reports、inputs、subscriptions)、`POST /intake/tickets/<id>/stop`、`POST /intake/tickets/<id>/retry`、`POST /intake/tickets/<id>/reply {text}`(人回答 attention)
- `GET /intake/threads/<id>`、`POST /intake/triggers/<id>/reevaluate`
- `GET/PUT /intake/policy`(规则、模式、名单、准入表;每次 PUT 新版本)、`GET /intake/policy/versions`
- `GET/PUT /intake/sources`、`POST /intake/requests {text, repo?, version?}`(工作台直接请求)
- `GET /intake/task-candidates`、`POST /intake/task-candidates/<id>/accept`

**工作台**:加一个"待处理"首屏,只列需要人的行;其余按 Source / Trigger / Eval / 执行 / 设置分页,和现有批次视图并列。

## 8. ABI 增量(v1 → v1.1)

| 操作 | 方向 | 内容 |
|---|---|---|
| `report` | harness → runtime | `{state, text?, attention?:{question, options?}, deliverable?:{branch, patch_sha256}}`;写入事件流 |
| `input` | host → runtime → harness | 追加 `/run/aios/inbox.jsonl`;harness 读;runtime 记事件 |
| `ticket_get` | harness → runtime | 返回 `/work/ticket.json` 的内容与 Mandate 的 scope、规则正文 |

种子 harness 的改动只有:启动时读 ticket,结束前发一次 `report`,循环里每轮看一眼 inbox。

## 9. 不做的

- 不做常驻 LLM Router:v1 Gate 是规则代码,Assessment 按候选批调用一次模型,没有持续对话的"主会话"。
- 不做跨 Source 身份映射,不做飞书,不做 auto-merge(合并永远是人)。
- 不在沙箱里放任何账号凭证,不让沙箱直连 Gitea。
- 不让接单层打分或改 harness。

## 10. 分期

1. **v1**:Source `workbench` + `gitea` 评论;Gate + Assessment;observe 默认;派工到沙箱;`report` ABI;中继开 PR;待处理首屏。验收:在 fc-node 仓库的一个 issue 下 `@cairn` 提请求,得到一个 PR,PR 首行声明自动产出,沙箱内无凭证(`grep` 不到令牌),事件流里有 report。
2. **v2**:CI 结果订阅;Watchdog 报警;重启恢复;task_candidate 产出。
3. **v3**:auto 模式 + 规则;探测派工接任务内循环。

## 11. 需要用户定的

- 第一个仓库是 `William2333ZZ/cairn` 本身(D8 的立场),agent 账号名建议 `cairn-bot`。
- Assessment 用 `deepseek-flash`,每次判定预算上限 0.01 USD。
- 单工单默认预算 1 USD、30 分钟、workbench 网络模式。
