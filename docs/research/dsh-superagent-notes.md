# DeepSeek Super-agent(dsh-superagent)调研笔记

日期:2026-11-01。来源:仓库 `deepseek-ai/dsh-superagent-prototype`(MIT,2026-10-31 开源,docs/000–008)、DeepSeek Harness 团队公众号文章《Super-agent 设计探索:迈向更自主的 Agent 系统》(原文未公开抓取,依据爱范儿、第一财经、量子位等转述)。仓库自述"研究原型,不是产品,与内部系统和文章并不一致,文章里一部分设计是未来方向"。

## 1. 它要解决的问题

- 模型会写代码了,但**人的注意力没有随之扩展**:一个人同时只能盯 3–5 个会话;CLI/MCP 没解决串行执行、多任务上下文混杂、本机下线任务中断。
- 结论:瓶颈从"模型能力"转向"人的注意力与管理"。会话应该变成内部实现细节,人只负责目标、决策、验收。
- 文章自认没有新理论:调度、缓存、事件驱动、存算分离这些经典系统概念搬到 agent 上。

## 2. 架构(文章层)

| 概念 | 内容 |
|---|---|
| 双层 Router / Worker | Router 常驻,全局协调,唯一与人对话的层,工具少、上下文小,可用轻模型;Worker 各有自己的上下文、工具、目录,处理 bug 复现、代码审查、功能开发,可再开 subagent,只回报关键结果 |
| 事件驱动 | Trigger = 群消息、邮件、日程、GitHub 评论、CI 结果;两级过滤:规则先丢弃无关事件,模型再按上下文判断是否行动 |
| 订阅直达 | Worker 接手一个 PR 之后,后续评论、状态变化直达该 Worker,不再经过 Router(消融:去掉直达,Router 决策 +30%,cache-read token 23.23M→31.77M,+37%) |
| 异步 ReAct | 长工具调用后台跑,前台会话继续收消息,人可中途改向或停止(已在 DSH 实现,默认关闭) |
| 存算分离 | 执行循环在云上、状态在数据库;代码与工具留在用户设备,由轻量 daemon 暴露;设备掉线再上线任务续跑 |
| Watchdog | 独立的监督者,看进展、识别无效循环与目标漂移,尝试修复 |
| 投机执行 | 借自 CPU:不等人问就先做,只把有价值的结果浮出来;内部用法"静默复现并修复":监控群聊,复现 bug,找到简单修法就提 PR,否则不出声 |

数值(厂商自报,合成消息流,约 300 条消息含 20 个真实请求,每配置 5 次):任务完成 p50 12.4 min、p95 15.7 min;单会话+subagent 基线 p95 29.5 min,且中性提示下模型 5 次都没调过 subagent。事件率 1→40/min 时批处理让单请求延迟只从 2.5 s 升到 3.9 s。**他们明说只测了调度协调,没测编码质量。**

公开承认的难题:跨周跨月的长期记忆;安全,云端循环持续接外部事件,一旦被注入或服务端入侵,所连的全部端侧设备都可能被控;大量并行任务下的进度可见性与人工审批。

## 3. 原型的领域模型(仓库 docs/002–003)

数据流:`Source → Trigger → (订阅扇出到已有 Ticket | Eval) → Decision → Ticket + Mandate → Assignment → Executor → Report → Channel 回帖`。

- **Source**:事件流的实现(GitHub、飞书),自己保存读位置、过滤自己的消息和范围外事件,把 Trigger 映射到 Thread 与回帖地址。
- **Eval**:无状态,两层:确定性 **Gate**(发送者、白名单、准入表)+ 模型 **Assessment**。输出 Decision ∈ {accept, stop, ignore, unavailable}。一个 Thread 同时只跑一个 Eval,期间到达的 Trigger 并入下一批。
- **Mandate**(授权)不可变:记录策略版本、范围、强度(explicit/inferred)、命中的规则、发送者身份。只有三种操作能改它:accept 创建、人的 stop 收回、策略重验证失败吊销。扩大范围要开新 Ticket。
- **Ticket**(工单)只能由 Eval 创建,状态 open→ended,人在它的 Thread 上再说话就恢复(机器人与 CI 不能唤醒)。Ticket 自己定义状态,**不镜像执行器会话状态**。
- **Assignment**(派工):Ticket 与一个 Executor 实例的绑定,pending→running→ended,一个 Ticket 可多次派工(重试在同一会话、目录、分支、PR 里继续)。
- **Report** ∈ {progress, waiting, ready, blocked, failed, dropped};"等人决定"是结构化字段,不从自由文本推断。
- **接受模式** off / observe(默认:正面研判只记录"本会接单",不执行)/ auto。主动工作必须命中至少一条团队写的规则。
- 原则:策略由宿主代码强制,消息、评论、引用文档只是数据,**不能改变权限**;身份不跨 Source 映射。

执行层细节:每个 Ticket 一个 `worker-<id>` 会话和目录;并发槽位 + 1 个给人的消息预留;后台作业每轮 30 分钟上限;Ticket 结束时 subagent 失去工具;重启后给执行器"恢复提示"(先查目录、git 历史、GitHub,再做外部动作)。**Worker 以操作者的 OS 用户直接跑命令,不需审批;"blocked commands" 明说不是安全边界。**

提示词:团队只能改三段(沟通、工单范围、交付要求),其余内置段可覆盖;**每次保存都是不可变版本**,默认更新时面板标出差异让人选"保留/采用";优先级:工具与宿主强制的事实 > 内置段 > 命中规则 > 团队段 > 外部文本(只是数据)。交付诚实:开了 PR 不等于 ready,草稿必须标草稿,UI 改动要真截图。

工程:TypeScript,Cordis 插件(DSH"一切皆插件");`src/core` 只有领域规则不做 I/O;单个 SQLite;Effects 持久队列(assessment/launch/delivery/relay/notice/reply,按 key 合并、退避重试);Passes 每 2 秒一轮。

## 4. 和我们在做的东西的关系

它和我们**不是同一层**。Super-agent 是宿主侧的"接单与派工"层;我们在做的是 agent 跑在里面的那台"机器"(microVM 里的五层 CairnSandbox)以及评这台机器的评测器。两边可以直接拼接:

| Super-agent | 我们(architecture v0.2) | 说明 |
|---|---|---|
| Worker 会话 + 目录 | 一个 Firecracker sandbox | 它们一个 Ticket 一个目录、同一 OS 用户;我们一个任务一个 microVM。它承认的安全难题(被注入后端侧全被控)正是 DSec 和我们的 Landlock + 无直连出网要解决的 |
| Router(轻模型,少工具,只路由) | 控制面 + 工作台 | 我们目前的 CP 没有"决策"能力,只有 API;Router 是可以加在 CP 上面的一层 |
| Eval:Gate + Assessment | 还没有 | 任务进入我们系统目前靠人手工建任务集;接单的两级过滤值得照搬 |
| Mandate / Policy 版本 | 预算 + ABI 事件 | 我们有预算与内核 ABI,但没有"谁授权了这项工作"的记录 |
| Watchdog | 评测器 + ABI 事件流 | 我们的事件日志(model_call、budget、exit)就是 Watchdog 的输入;我们还没写判定无效循环的那个监督者 |
| 投机执行 | Harness² 的 fork Probe | 同一思想:先做,结果有价值才呈现。我们在 sandbox 里 fork 快照来探,比它便宜且可回滚 |
| Report 六态 | 评测器的 score / infra_fail | 我们缺一个 agent 自报状态的词表 |
| 提示词的不可变版本 + 默认更新对比 | CairnSandbox 版本 = runtime hash + langs 层 + harness 层 | 它只版本化提示词,我们版本化整个 harness 层;思路一致,我们更彻底 |
| observe → auto 模式 | 候选版本 → 提升(promotion) | 都是"先只记录,看对了再放开" |
| 内部实例在迭代 DSH 自身 | D8:任务来自我们自己的工程 | 完全相同的立场 |

它**没有**的东西:自我改进(没有任何 RSI 回路,harness 固定,只有提示词可配)、隔离(不是安全边界)、评测(只测了调度)。我们**没有**的东西:事件驱动的接单层、授权模型、Worker 的后台作业/槽位/重启恢复这些运维细节。

## 5. 建议吸收的点(按优先级)

1. **接单领域模型**直接用到工作台的任务流:Trigger/Thread/Ticket/Mandate/Assignment/Report 这组词定义清楚,我们的"任务来自用户自己的工作"才有载体。先只做一个 Source(Gitea 的 issue/PR 评论),Eval 用 Gate + 模型两层。
2. **Report 六态**作为 guest 里 harness 向 ABI 自报的状态(progress/waiting/ready/blocked/failed/dropped),评测器与 Watchdog 都消费它。
3. **observe 模式**:主动工作先只记"本会接单",这和版本提升的 shadow 评测是一回事,共用一个开关。
4. **Watchdog**作为宿主侧独立进程读 ABI 事件,判定无进展/重复循环,先只报警不干预。
5. **DSH 本体作为 TS harness 候选**:DeepSeek Harness 是 TS、Cordis 插件化、8 月开源,工具箱完整(文件编辑、持久 shell、检索、Skills、计划模式、subagent)。放进 `/aios/harness/ts` 的候选表里,和 pi 并列对比体积与可裁剪性。
6. 不照搬的:同机同用户跑 Worker;Router 单点常驻 LLM(我们先用规则 + 评测器,等任务流真有量再谈)。

## 6. 来源

- https://github.com/deepseek-ai/dsh-superagent-prototype(README、docs/000–008)
- 爱范儿 https://www.ifanr.com/1687026;Venture Atlas https://www.ventureatlas.org/news/2026-10-31-deepseek-superagent;chinamodelapi https://chinamodelapi.com/news/deepseek-superagent-open-source;腾讯新闻 https://news.qq.com/rain/a/20261031A0DMXE00;PANews https://www.panewslab.com/zh/articles/01a1928e-5db7-7202-9a3f-794f3f0e1a2d
- DSH 用法转述 https://www.orcarouter.ai/blog/deepseek-dsh-usage
