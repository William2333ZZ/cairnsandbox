# 005 Agent 服务:对话、接入、接单与授权

本文说明用户的一句话怎样进入对话、被路由、变成一个 Ticket,谁可以让它变成 Ticket,这份授权能走多远,以及例行、批准、记忆怎样挂在同一个服务上。它建立在 [002 领域模型](002-domain.md) 之上,实现 [001](001-journey.md) 的第 2(接入)、3(交代)、5(被找)、6(纠正)、7(例行)、8(它自己来活)、9(记忆)阶段里宿主侧的部分。

## Agent 服务持有什么

Agent 服务是宿主侧的一个常驻服务(容器 `fc-intake`,旧名接单层),每个用户名下持有:Conversation、Connector(Source + Channel)、Router、Eval、Ticket 与 Assignment、Routine、Approval、Memory、Relay、Watchdog。它没有每个 agent 的常驻进程:agent 的"活着"只是这些表和一个每 2 秒一轮的 pass;事做完,沙箱就还回去。所有状态在一个 SQLite 里(多用户时换 Postgres,表不变),重启后从表恢复:运行中的派工重新派,待定的 Approval 保持待定。

## 对话与路由

每个 Agent 一条 Conversation。进入它的一切都先过 Router,三步,按序:

| 步 | 规则 |
| --- | --- |
| 去重 | 同一 Trigger id 只处理一次;Connector 重放、重启重读都落在这里 |
| 归属 | 发送者在某个在途 Ticket 的 Thread 上说话,或在工作台里对着某件事说(composer 选中了那件事),就是那件事的 Input,不进 Eval;其余进 Eval |
| 合并 | 同一发送者 10 秒内的连续几条合成一个 Trigger 再进 Eval 或 Input;"停"不合并、立即生效 |

Conversation 里只有四种条目。用户的 message 带来处(哪个 Connector、哪个 Thread);agent 的 say 带去处——消息从哪来,回话就送回哪,并且总在 Conversation 里留一份;一件事开始时留一个 work 标记,点开是它的时间线;系统的 note 记接入、例行生效这类事。say 只能由 006 的"说话的规矩"产生,Agent 服务没有别的写 say 的入口。



宿主代码强制白名单、规则与 Mandate。消息、评论、引用文档是判断的输入,不能改变权限。

## 对话里关于它自己的话

有几句话不是请求,是对 agent 本身说的,宿主直接办,不开单(`intake/commands.py`,Router 在认出停止与退回之后、认例行之前看):

| 说法 | 办什么 |
| --- | --- |
| "把每天的预算改成 10 美元" / "daily budget to $10" | 改策略里它自己发起的事的每日上限(0.1 到 1000 美元),对话里留一条"已改:……(原来 ……)" |
| "每件事的预算设为 $2.5" / "per-job budget to $2.5" | 改每件事的预算(0.05 到 100 美元),已在做的不受影响 |
| "你现在接了哪些?" / "状态?" / "status" | 答一份清单:接入、例行、预算与今天已花、运行系统及是否钉住、教过的规则条数、其他 agent、该问就问的读数 |

只认名单里的人(`trusted`)并且是对 agent 说的话;陌生人在 issue 里说"把预算改成 1000"什么也不会发生。措辞是确定的几种,**一句请求里碰巧提到预算("估算一下每天的预算")是请求**,不是命令。答案与改动都是对话里的 note,所以它同时是记录。

## 两条路

**Eval:决定是否开始工作。** Source 标记一条消息是否点名了 agent(提到 bot 账号,或工作台直接发)。Gate 查发送者与该 Source 的 Policy:

- 名单内的人点名 agent,是明确请求。
- 名单内的人在已有 Ticket 的 Thread 上继续说话,归那个 Ticket。
- 其他人的消息按准入表的格子处理。
- bot 名单上的 bot 只能产生推断候选,其余 bot 不受理。

**订阅:继续已有的工作。** 订阅 Thread 上的每条消息都扇出到它的 Ticket 作为上下文,包括 bot 与 CI 的消息。这条路绕过 Gate,不授予新权力,Ticket 仍受 Mandate 限制。人在 Ticket 自己的 Thread 上的消息由该 Ticket 的执行者决定:在范围内就处理,超出范围就交还一次。交还后无人认领的消息才进新的 Eval,并附上执行者的理由。

只作上下文的订阅(相关的 issue、另一个 PR)送消息但不给权力,人在那里说话不会唤醒已结束的 Ticket。

## 例行与自动 Source:不经用户开口的事

**Routine** 属于用户。用户在对话里说"以后每天早上八点……",agent 把它作为一个 `routine` 类的 Approval 提出来(固定文本、触发条件、预算),用户同意后生效;改和停也走同一条路。到期或事件发生时,Routine 以系统身份产出 Trigger,走下面自动 Source 的 Gate → Ticket;做完按 006 的规矩找用户。策略里管理员写的 `schedules` 是 Routine 的第一期形态,迁移时成为该用户名下已批准的 Routine。

人不是唯一的请求者。三类 Source 以系统身份(`system`)产出 Trigger,走同一条 Gate → Ticket 的路;它们在各自 Source 的白名单里,强度 explicit,但只有策略里对应的规则启用时才接(每类一条规则,默认启用 `ci-failure`,其余 observe):

| Source | 触发 | Trigger 文本 | Thread |
| --- | --- | --- | --- |
| `ci` | main 上一次 CI 失败(轮询提交状态) | 失败的步骤名与日志尾部 | `ci:<commit>` |
| `evaluation` | 迭代驱动、审查工作流经 `POST /triggers` 推送:一轮结束时失败最集中的任务、critic 拒绝的原因、审查确认的发现 | 任务 id、失败轨迹摘要或发现正文 | `evaluation:<batch 或 workflow id>` |
| `schedule` | 到期的定时请求(间隔或每日时刻,存在策略里) | 定时项的固定文本 | `schedule:<id>:<日期>` |

自动 Source 的 Ticket 和人发的一样回帖(到工作台会话,`ci` 的还评论到提交)、开 PR、等人审阅;它们的 Mandate 记下来源规则,预算取规则里的值。

两条从真实使用里学来的约束:`ci` 同一时间只开一张(已有未结束的 CI 单时不再为下一个红提交开新的,那一张结束后才看当前的 main),预算 0.3 美元——一天里 CI 红了六次,六张单各烧五毛都停在步数上限,是在花钱制造沉默的僵尸单;自动 Source 的日志行不进别的事的记忆(只有同一类来源的事能看到),空的笔记不记。

**每日上限**:自动 Source(`ci`、`schedule`、`evaluation`)发起的事,在今天(本地午夜起)所有派工的花费合计达到策略 `defaults.daily_budget_usd`(默认 5)之后,Gate 以 `daily-budget` 为由忽略它们,并在对话里说一次;人要的事照常做,只是确认里多一句"今天已经超过上限"。一天里 CI 红六次、六张单各烧五毛,是这条上限存在的原因;`GET /health` 带 `spent_today` 与 `daily_budget_usd`。

## 强度

| 强度 | 情形 |
| --- | --- |
| explicit | 名单内的人点名 agent,或在已有 Ticket 的 Thread 上跟进;准入表格子设为 explicit |
| inferred | 名单外的人在 Ticket 的 Thread 上跟进;准入表格子设为 inferred;名单上的 bot 点名 agent 或出现在全收范围 |
| 不受理 | 其他 bot;准入表忽略的一切;接受模式 off 下的全部 inferred |

## 准入表

每个 Source 一张,行是接收范围,三列是(名单内未点名,名单外点名,名单外未点名),格子取 explicit / inferred / ignore。

| Source 与范围 | 名单内未点名 | 名单外点名 | 名单外未点名 |
| --- | --- | --- | --- |
| gitea:issue 评论 | inferred | ignore | ignore |
| gitea:PR 评论 | inferred | ignore | ignore |
| gitea:新 issue、新 PR | inferred | ignore | ignore |
| gitea:默认分支 CI | — | — | 只作上下文 |
| workbench:直接请求 | explicit | — | — |

把"名单外点名"设为 explicit,这个范围里任何人点名 agent 都能派活,适合专门的支持频道。收紧格子不立刻停已开的 Ticket,但能在重验证时吊销其 Mandate。

bot 的识别:`[bot]` 后缀的登录名、Gitea Actions(`fc-runner`)、Source 声明的机器人。用普通账号的自动审查者算人,团队提示里要说明它们是自动的。

## 接受模式与规则

| 模式 | 行为 |
| --- | --- |
| off | 不产生 inferred 候选 |
| observe(默认) | Assessment 判"接"也只记录 `would_accept`,不执行 |
| auto | 接受 inferred 候选,且必须命中至少一条启用的规则 |

explicit 请求在三种模式下都执行。从 observe 开始,看过记录的 Decision 再切 auto。

规则是团队写的自然语言,每条有名字,描述一类主动工作,例如 `reproduce-and-fix-simple`:issue 里报了可复现的 bug,修法不超过 200 行且不改接口,就提 PR;否则不出声。每次 Assessment 对每条启用的规则判一次。Mandate 记下命中的规则名,执行者收到规则正文;规则里的流程与沟通要求对该 Ticket 覆盖默认。对所有 Ticket 都适用的要求写在团队提示段,不写进规则。

## Mandate 里的三项执行参数

接单层把预算(USD)、网络模式、Cairn 版本写进 Mandate,而不是执行设置:它们决定这件工作被允许花多少、能碰什么、用哪个版本跑,是授权的一部分。默认值来自 Policy:1 USD、allowlist、当前稳定版;工作台可以为一个请求指定候选版本,这是任务内循环的入口。

## Policy 版本

版本 = 规则 + 接受模式 + 各 Source 部分的内容哈希。每个 Mandate 记下它被接受时的版本。

版本变化时:丢弃运行中的 Eval,重排其 Trigger;给受影响的在途 Ticket 发一条 `policy_changed` Input,说明命中规则是否改了或不再适用、请求者是否还能派活、主动工作是否还被接受;不受影响的 Ticket 不收通知;不立刻吊销 Mandate。

Source 配置与 Policy 分开:前者决定什么进入系统,后者决定谁能派活。回帖在 Source 关闭时等待,Source 不再监听目标时丢弃。执行者对自己结果能做什么(比如何时可以合并)是 harness 指令,不是 Policy。

## 恢复、停止、重验证

已结束的 Ticket 保留 Mandate。人在它自己的 Thread 上说话,它不经 Eval 直接恢复,无论谁说的;bot 与 CI 不唤醒它。

Mandate 只有三种变化:accept 创建,人的 stop 收回,重验证失败吊销。人停的 Ticket 保持结束,那里再派活要新的 accept。

恢复前重验证:Source 仍在监听原 Thread;explicit 跟进的 Mandate 通过;inferred 的要求当前模式为 auto 且命中规则仍启用;准入表格子来的要求在当前表与名单下仍至少有原强度;bot 的要求 bot 仍在名单;其余要求发送者仍在白名单。失败则 Ticket 保持结束,等待的消息按"没到任何 Ticket"处理。

- 人在 Thread 里说"停"是普通 Trigger,由 Eval 判 stop;执行者不能宣称人让它停。
- 面板上的停是命令,直接作用于 Ticket,记为人工停止。
- 需要等待的条件(比如"N 小时没人认领")是一个会等待并再查的 Source,Eval 从不等待。

## Eval 没完成

时限从候选送达时起算。超时的 Eval 重排其 Trigger;连续两次重试后第三次超时记 `unavailable`;Assessment 失败也记 `unavailable`;Policy 变化或按过期 Policy 做的决定记 `unavailable` 并立即按当前版本重排。

面板只在 unavailable 的 Decision 不到 24 小时、仍是其 Thread 最新 Decision、未被忽略、且 Thread 不在队列或评估中时要人处理:重新评估或忽略。

## Assessment 的成本

Assessment 经模型代理调用,用接单层自己的令牌与预算(每次上限 0.01 USD),计入接单层而不是任何 Ticket。Gate 不调模型。

## 批准

批准表(009)里的四类动作沙箱做不了。Ticket 的 harness 以 `report waiting` 加 `attention{kind: approval, action, draft}` 提出;宿主在预算将超时自己提出 `budget` 类;Routine 的新建与修改是 `routine` 类。Agent 服务为每个创建一个 Approval,进对话(say approval,送回来处),Ticket 进入"等人 / 批准"阶段。用户同意,Relay 执行(合并 PR、发出消息、追加预算、写入 Routine),结果作为 Input 送回 Ticket;否决也作为 Input 送回;Ticket 结束,未决的 Approval 失效。Relay 对同一 Approval 的执行幂等:决定先"认领"(状态 `deciding`),两次点击只执行一次;动作失败不算批准,草稿回到 pending 并带上错误,可以再试;合并按审查时看过的头提交钉住,PR 在之后动了,草稿同一行重新审查、头换成新的。harness 起草的 send / budget / routine 只带种类与文字,目标(来处渠道)、数额(从文字里读)、日程(按例行的说法解析)由宿主补全,补不全的审查拦下。

## 记忆

Memory 由 Agent 服务持有,harness 只经 ABI 读写自己这件事的 notes。两个时刻:

- **派工开始**:宿主把 profile 全部、log 里与这件事最相关的 30 条(按仓库、路径、关键词重合;后期按向量)、同一仓库上未结束的 Ticket 的 notes,写进 `ticket.json` 的 `memory` 段,标为"信息,不是指令"。
- **派工结束**:宿主用这次派工的轨迹摘要、Report 与当前 profile 调一次模型(走模型代理,记在该用户的预算下),得到对 profile 的增删改;只能删除这次给它看过的事实;log 追加一条(日期、仓库、做了什么、结果)。

用户在工作台能看到四层、改事实、删条目;删了就不再进任何派工。Gate 与 Assessment 不读记忆。

## 交接

harness 在发现这件事属于用户的另一个 agent 时(ticket 里有 `agents` 名单,提示里写明怎么交),以 `HANDOFF: <名字>` 加简报结束,上报为 `waiting` + `attention{kind: handoff, to, brief}`。宿主(`intake/handoff.py`)不让 harness 直接碰别的 agent:它检查有没有这个 agent、不是自己、不成环、不超过三跳,通过了就在接收者的 Engine 里 `receive` 一条 workbench 触发(文本 = 简报 + 人最初说的话;`meta.handoff` = 谁交的、哪张单、链;原渠道随触发带过去),交出者的 Ticket 先只打上"在交接"的标记(不能再提草稿)并在对话里留一条 note;**接收者的 Gate 接下了**,才把它结束(`handed-off`)。接收者没接下(策略忽略、或十分钟没有回音)就以 `stuck` 告诉用户"想交给 @x,它没有接下:原因,要我自己做吗?",Ticket 留在原处、带着这个问题。检查没通过的(没有这个 agent、成环……)同样以 `stuck` 告诉用户"想交给 @x 但交不出去:原因"。接收者的 `say` 经 `mirror_handoff` 同时留在交出者的对话里。两个 Engine 的锁不嵌套(接收在独立线程里做),互相交接不会互等。接缝的定义在 002 的 Handoff。用户不必写 `@名字`:交接动词("交给 / 转给 / hand off to / assign to")后面跟着一个已有 agent 的名字,Router 就把整句话送去那个 agent(`Engines.route`);"请 fix 一下"这种不带交接动词的不算。

## 纠正

用户扳回 agent 做错的事,是这个产品里最值钱的学习信号(D17):它比"做完没有"多一个信息,就是**该怎么做才对**。认出纠正的是宿主,不是 harness(harness 会被它自己改)。五个入口:

| 入口 | 认出的办法 | 带什么 |
| --- | --- | --- |
| 插话(`redirect`) | 对一件已经做过什么的 Ticket,人的消息含"不对 / 不要 / 应该 / 改成 / don't / instead of"这类词(`CORRECTION_RE`);对它的提问的回答不算 | 原话;agent 当时的 Report |
| 否决草稿(`denied`) | 批准被否决,工作台的否决可附一句理由 | 理由;草稿 |
| 退回(`returned`) | 工单的 PR 没合并就关了 | 只有信号 |
| 停止(`stopped`) | 人在事做到一半时停下 | 只有信号 |

有原话的(插话、带理由的否决)经一次便宜的模型调用提炼成**一条规则**:一句祈使句,说在什么情形下做什么或避免什么,语言随原话;模型可以判定"只关这一件事"(比如"周四再做"),这时不记。模型调用失败或没有模型时,规则照原话记,前面加"用户纠正过:"——宁可留下原话,也不丢纠正。提炼在后台线程里做,不占 Router 的锁。

规则进 Memory 的 `rules` 层(同一句不重复,最多 60 条,超出丢最旧的),对话里出一条 note:"记住了:……"。派工开始时 `ticket.json` 的 `memory.rules` 带最多 30 条:本仓库的优先,别的仓库学到的不带过来;harness 把它们放在请求之后,说明"除非这件事的请求另有说法,照做"——规则是用户教的,但这次的话仍然比它大。用户在记忆页能看到每条规则的来处并删除;删了就不再带。

**规则有牙**(TRACE,2026):只放在记忆里的纠正是建议——agent 读到了,照样可以做错(论文里加了记忆层之后,偏好检查仍有五成多被违反)。所以能写成"对这份改动的检查"的规则,提炼时由模型顺手给出一条**检查**,宿主校验后和规则存在一起:`path_forbid`(改到的路径不能匹配)、`new_file_regex`(新文件的路径必须匹配)、`added_forbid`(新增的行不能匹配),都是确定的正则,没有模型写的代码在跑;太宽的(匹配空串、或匹配所有样本路径)直接丢,规则作为建议留下。有检查的规则在**完成关**上执行(`rule_gate`):harness 报 ready 带补丁时,宿主拿补丁跑这些检查,违反了就不算完成——违反处作为输入(来自 system)送回 agent,再跑一次,最多两次,对话里留一条"提交前被拦下一次";两次之后仍违反,草稿照样到用户面前,审查里带一条"违反了你教过的:……"。记忆页和"记住了"那条 note 都会说这条规则提交前会被检查。

纠正还是评测的原料:这件事之后若 PR 合并,挖出的任务带 `kind: dependent` 和这些规则(007)。"没问就错了"的纠正(Ticket 在纠正前没有提过问题)计入"该问没问"(006)。

## 尚未实现

纠正的认出是词表加位置的规则,会漏掉不带"不对"这类词的纠正,也会把偶尔含这些词的补充当成纠正(代价是多一条可删的规则);规则的作用域只分"本仓库 / 用户",没有按路径或语言分;关 PR 与停止不问原因。按 001 与本篇:Thread 并成一段对话(对话表、Router 的归属与合并、say 送回来处已有);Routine 的识别只认"每天 / 每 N 分钟 / 每小时 + 时刻"的说法,每周与自然语言条件("CI 红了就")不识别;记忆的相关度只按仓库与时间,没有向量;多用户的 Postgres。其余:CI 结果作为上下文(`gitea` Source 只读 issue、PR 评论与新 issue);准入表里"名单外点名 = explicit"之外的格子组合未经真实流量验证;Assessment 的提示只有一版;策略变更对在途 Ticket 的 `policy_changed` Input;执行者把消息"交还"Eval 的那条路(执行者判定超出范围后,消息现在留在工单里,不会回到 Eval);`addressed` 是子串判断。
