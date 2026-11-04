# 006 执行

本文跟随一个被接受的 Ticket 走过派工、报告、交付与恢复。它建立在 [003 沙箱与平台](003-sandbox.md)、[004 沙箱内系统](004-guest.md) 与 [005 Agent 服务](005-intake.md) 之上。

## Ticket 的阶段

每个 open 的 Ticket 有且只有一个阶段,由它在等什么决定。从上往下读,第一个匹配的行生效。

| 阶段 | 条件 | 面板标签 |
| --- | --- | --- |
| 等人 / 送达 | 一条 Input 没能送进沙箱;2 秒后重试,之后退避到一分钟 | 输入未送达 |
| 等人 / 批准 | 有一个 pending 的 Approval | 等待同意 |
| 等人 / 回答 | 最近的 Report 带 attention(问题) | 等待回答 |
| 等人 / 审阅 | 最近的 Report 是 ready | 等待审阅 |
| 等人 / 受阻、失败 | 最近的 Report 是 blocked 或 failed,且此后没有人的消息到达 | 受阻 · 失败 |
| 工作中 / 排队 | 没有 Assignment,或有一个 pending | 排队 |
| 空闲 | 最近的 Report 是 waiting,或 Assignment 已结束 | 等待 Trigger |
| 工作中 | 其余 | 工作中 |

"等人"意味着没有人的输入 Ticket 不能继续。人的新消息把 blocked 或 failed 的 Ticket 放回给执行者。

## 对用户说话的规矩

001 第 5 阶段:只在做完、卡住、要做决定时找用户。agent 对用户说的话(say)只由下面的事产生,harness 没有别的途径向用户说话:

| 事 | say | 送到哪 |
| --- | --- | --- |
| Eval 接受 | ack:一句"接到了",附短名 | 来处 |
| Report ready | ready:一句结果加交付物(PR、改动摘要、测试结果) | 来处与 Conversation |
| Report blocked、failed | stuck:卡在哪、试过什么 | 同上 |
| Report waiting 带 attention(问题) | question:一个具体的问题 | 同上,并进工作台的"需要我" |
| Approval 创建 | approval:将要发生的不可逆动作的草稿 | 同上 |
| 一件事跨过一个自然日仍在做 | digest:一天一句进展 | Conversation |
| 晋升 | update(007) | Conversation |
| Report waiting 不带 attention(等限额、等时间)、progress、dropped | 不说 | 只进时间线 |

同一件事同一个问题只问一次;用户答过的进 Input 与 notes。所有外部写都由 Relay 做,沙箱连不到任何 Connector。

不可逆动作——合并 PR、向用户之外发消息、超出预算的花费、新建或改例行——沙箱只能提草稿:开 PR 是合并的草稿;其余写进 attention 的 approval 字段。执行发生在用户同意之后(005"批准"):合并、向外发消息(代码托管评论或聊天)、加预算、写入例行,都由宿主的中继执行,凭证只在宿主。

人的 Input 优先于排着的工作:harness 在模型调用进行中每秒读一次收件箱,人的话一到就放弃该次调用、带着它重新规划(事件 `interrupted`;被放弃的调用结束后费用照常计入);停止立即生效,不经 Input。

harness 没做完就停下(`waiting` 却没有 attention——步数或时限到了,常常是真实使用里最安静的失败)不是"等人",也不能让单子开着不吭声:宿主把它当 stuck 说出来("停在了上限上,事没做完"),单子带上"要继续吗"的问题进入"等人 / 回答";用户回"继续"就是一次插话,单子接着做,不是纠正。

harness 还有第四种结束方式:`HANDOFF: <agent>`(005 交接)——这件事不是它该做的,交给用户的另一个 agent;它之后不再说话,也不能再提草稿。

## 对话要送多少

模型每次调用都把整段对话重新发一遍,所以一件长事为每一次旧的 `cat` 付了一遍又一遍的钱:在一个真实仓库上,一次运行 35 步花了 175 万输入 token,停在每次运行 0.5 美元的上限上,事还没做完(用户自己的历史题上的基线里,一半的运行都是这样结束的)。`aios/model.py` 的 `compact_history` 在发给模型之前把**旧的**命令输出缩成头尾两段并注明"被略去的部分重跑命令就能再看到";任务、模型自己的话与调用、最近八条保持原样,harness 记下的轨迹不受影响。**默认关着**:把它打开的第一个候选(人写的,走 `evolve.py candidate`,11 道题 k=1)每次调用的输入从约 4.3 万 token 降到约 2.7 万,但同样的钱换来的是更多步(58 步对 36 步)而不是更早做完——平均分 0.693 对 0.651(区间包含 0),每次运行花费 0.451 对 0.425:没有被接受,归档。关着是因为"没被接受的改动不进种子"。

## 该问就问

harness 在卡住时以 `QUESTION:` 结束(至多问一次),这是对用户说话的三种终态之一;**什么时候该问**是 harness 的能力,不是外层能强制的——外层只管得住它不乱说,管不住它该问不问(D17)。所以两头都要有尺子,同一把:

- **离线**(007 的 obstacle 题):问的问题里有几成是真障碍,真障碍有几成被问到,合成 Ask-F1。
- **真实使用**(`GET /ask`,按 agent 版本分):用户回答了并给了实质内容的问题是**有用的问**;回答"你定 / 随便"一类的是**不该问的**(打扰);工单没问过、用户后来纠正它的(005 纠正的 `redirect` 且问前没有 question)是**该问没问**。精度 = 有用 / (有用 + 不该问),召回 = 有用 / (有用 + 该问没问)。两个数一起看:多问会拉低精度,猜错会拉低召回。Update 把候选在 obstacle 题上的 F1 变化说给用户。

**第二双眼睛**(《Ask or Assume?》,2026):agent 不知道缺什么时,会一直翻下去直到步数用完——第一次真实走"该问就问"时就是这样:"把默认预算改一下"没有问改成多少,翻了一圈代码,停在步数上限上。该论文的做法是把"判断缺不缺信息"从干活的 agent 里拆出来,另放一个读者看任务状态,缺了就让主 agent 去问;同样的任务上解决率 69% 对 61%,且简单的任务问得更少。我们的读者在宿主侧(`intake/intent.py`,看门狗里,版本循环改不到它):一件事跑到第 10 步和第 24 步各看一次,用自己的一次模型调用读"请求 + 最近八步",**只在**事情卡在"只有人能说的东西"上时,给 agent 一条来自 host 的输入,要它立刻以 QUESTION 问出那个问题;它自己不向用户提问——到用户面前的仍是 agent 的 QUESTION。有进展、已经在问、已问过的,什么也不做。提醒次数计入 `/ask` 的 `nudged`。

## 派工到一台沙箱

一次 Assignment = 一台沙箱。

| 步骤 | 做法 |
| --- | --- |
| 创建 | `POST /sandboxes`:模板 debian-13;层 = Mandate 所指 Cairn 版本的 langs 与 harness 层 + 项目的 env 层 + Ticket 上一次派工打包的工作区层(如有);class persistent;网络模式与预算来自 Mandate;`PUT /budget` 写本地上限 |
| 准备 | 首次派工时宿主把仓库的浅克隆(深度 1,过滤 1 MiB 以上的 blob;超过 150 MiB 的检出拒绝派工)打成 tar 送进沙箱(`PUT /files`),沙箱内切到 `ticket/<短名>` 分支;再次派工时工作区层已含仓库,只送恢复提示 |
| 送入个人 harness 目录 | 这个用户的目录(宿主 `<state>/homes/<用户>`,他的所有 agent 共用;004 个人 harness 目录)里运行时读的文件打成 tar 送进 `/work/home`,root 持有、agent 只读(agent 不能改自己的规则;机器的路是宿主上的 proposals/);`tasks/`、`proposals/` 不送;目录为空就不送。摘要写进 ticket.json 的 `home` 与 Assignment 的 `home`,与轨迹里的 `harness_digest` 同一个值;环境变量 `CAIRN_HOME=/work/home`。`INTAKE_HOME_VOLUME=1` 时改走卷:每个摘要一个只读卷 `home-<摘要>`(从 tar 一次生成),创建沙箱时就附在 /work/home,派工结束后没人挂着的旧版本删掉;代价是带卷的沙箱冷启动(D35),默认仍是 tar |
| 送入 Ticket | `/work/ticket.json`:原始 Trigger、Thread 快照、Mandate 范围、命中规则正文、Input 队列、`memory` 段(005"记忆",标为信息) |
| 运行 | `POST /exec/stream {cmd:"/aios/bin/aios run --ticket …", user:"agent", landlock:true}` |
| 读报告 | 从 `GET /sandboxes/<id>/events` 读 `report` 事件;沙箱退出而无终态 Report,按退出码补 failed(非 0)或 ready(0 且有 deliverable) |
| 结束派工 | 打包可写层为该 Ticket 的工作区层;销毁沙箱;做一次记忆更新(005"记忆") |

沙箱里没有任何代码托管凭证,也连不到 Gitea。

## Input 的送达

Thread 上的新 Trigger(人的评论、CI 结果)扇出到订阅的 Ticket。沙箱在运行,就经 ABI `input` 追加到 inbox,harness 下一轮读到;沙箱已回收,就开一次新的 Assignment,从工作区层恢复,并附恢复提示:先看目录、git 记录与 PR 状态,再做外部动作。

一条消息算"已读",当它被某一步取用并随后有 Report 或回合结束。Ticket 结束时,未读、未确认的消息释放回 Eval。

## 槽位与时限

- 节点容量由控制面管;接单层另设并发上限,外加一个预留槽给"人的消息或状态请求",它优先于新派工。
- 一次派工 30 分钟;到时 Report 记 waiting,打包工作区,销毁沙箱。CI 结果等其他 Input 等普通槽。
- 启动并发进行,慢的准备只拖慢自己的 Ticket。

## 交付

| Report | 后续 |
| --- | --- |
| ready 带 deliverable | 宿主 `GET /files` 取出 git bundle 或补丁,中继用 agent 账号推到 `ticket/<短名>` 分支并开 PR;PR 描述首行固定为"由自动 agent 产出,非人工";回帖到原会话 |
| ready 无 deliverable | 结果文本回帖;Ticket 结束 |
| waiting | 释放槽;等下一条 Input |
| blocked / failed | 回帖一句原因;等人;transient 错误(网络、408/429/5xx)自动重试三次,退避递增 |
| dropped | 执行者发现无事可做;一句回帖;Ticket 结束但可被人唤醒 |
| progress | 不回帖;只在面板上更新 |

执行者对一个新请求确认一次,点明对象与预期结果,然后沉默到结果就绪、需要决定或无法完成。消息默认一句话,上限 100 字。开了 PR 不等于 ready;草稿标草稿。

合并永远是人:中继不开 auto-merge。

## Ticket 怎样结束

- ready 且未开 PR → 结束。
- 跟随自己开的 PR:全部合并或关闭 → 结束,结果为 merged 或 closed;没开 PR 的,跟随的 PR 全部关闭 → 结束。
- dropped → 结束,可被人唤醒。
- 人 stop → 结束,不再唤醒。
- 结束时打包工作区层,聊天 Channel 收到一句最终去向。Source 对 Ticket 的 Thread 在结束后继续轮询两周。

## Watchdog

接单层里每分钟一轮的检查,只读每台运行中沙箱的 ABI 事件流;第一版只报警(进"待处理"的系统问题,键 `watchdog:<工单>`),不干预。

| 信号 | 判定 | 动作 |
| --- | --- | --- |
| 停滞 | running 下 10 分钟无 model_call、无 exec | 面板告警;Report 补 blocked(stall) |
| 循环 | 最近 20 次命令重复率 > 60% | 告警 |
| 预算 | budget_charge 触顶 | runtime 已拒绝;记 failed(budget) |
| 越界 | Landlock 拒绝事件,或命令触及 Mandate 范围外路径 | 告警;计入作弊信号 |

## 失败与恢复

- 沙箱创建失败(429、503)按控制面的重试换节点;三次失败记 failed。
- 节点故障:persistent 沙箱从最近 bundle 在别处恢复;接单层把 Assignment 标 ended("host restarted"),下一次 Assignment 带恢复提示。
- 重启接单层:运行中的 Assignment 标 ended,未取入历史的 Input 保留;对账沙箱列表,找不到的沙箱按已回收处理。
- 回帖失败按 5 秒到 300 秒退避重试;同一会话的消息保序;会话不存在时丢弃并留记录。
- 存储的工作(派工、送达、回帖)先落库再执行,连续三次失败才升为系统问题。

## 与两个循环的接口

- **任务内循环**:Ticket 是"用户任务"的来源。工作台可对一个 Ticket 开探测派工:从主派工的检查点 fork 两台候选沙箱(不同的 harness 编辑),Compose 后只交付一个。用户在 PR 上的接受、拒绝、改动行数写回改进库条目的执行对照。
- **版本循环**:PR 合并且 diff 触及有测试覆盖的代码时,产出一条 Task 候选(仓库、提交、涉及文件、测试命令);评测器的挖掘步骤据此生成带隐藏测试的任务,人确认后进任务集。
- Ticket 的事件与轨迹格式与评测 Run 一致,工作台同一套视图。

## 尚未实现

按 001 与本篇:外发(send)只支持代码托管的评论。其余:Watchdog 的干预(注入提示、重开派工);"人的消息预留槽"只有一个;探测派工(依赖检查点带文件系统快照);PR 评论的 review 线程处理;基础设施失败的重试只按"同一人工输入之后的次数"计,没有按 Source 区分;Ticket 结束时未读消息释放回 Eval 只在恢复派工里做了一半(有未送达的人工输入就再派一次,而不是进 Eval)。
