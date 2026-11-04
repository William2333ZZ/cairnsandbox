# 产品与学术的现状(2026-11)与我们该往哪走

[always-on-agents.md](always-on-agents.md) 对照的是一个讲座;本文把视野放宽:2026 年同类产品分成哪几路、学术上这一年在争什么、哪些结论直接影响我们的下一步。写于 2026-11-01,出处见末尾,数字以原始出处为准。

## 一、产品:三条路线,没有人同时走三条

| 路线 | 代表 | 共同形态 | 没有的东西 |
| --- | --- | --- | --- |
| 云端编码 agent | Devin、OpenAI Codex cloud、GitHub Copilot coding agent、Google Jules、Cursor cloud agents | issue / 一句话 → 云沙箱 → PR;并行多任务;按任务计费(Devin 按 ACU,Copilot 按 premium request,Jules 免费 15 任务/天) | 没有"我的 agent":一事一会话,会话之间不记得你;不自己变强,靠换模型 |
| 个人 always-on agent | Grok Bot(SpaceXAI,$120/座/月,2026-08 开放)、OpenClaw(自托管,147k stars)、Claude Cowork / scheduled tasks、ChatGPT agent | 持久存在、有记忆、学例行、多渠道(Slack、邮件、IM)、能在云电脑上操作应用;Grok Bot 还有多 bot 交接与团队共享记忆 | 不改自己的运行系统;编码只是它会的一件事,不深 |
| 自我改进研究工具 | Karpathy autoresearch(2026-03,630 行;`program.md` 定目标,5 分钟一轮,只留改进;一夜约 100 次实验) | propose → run → evaluate → keep/discard;人只改目标文件 | 没有用户、没有渠道、没有记忆;只对一个脚本一个指标 |

我们的位置:路线一的沙箱底座(每单一台 microVM),路线二的产品形态(001 的十个阶段),路线三的自改进循环(007 的 evolve)。三者合在一起的产品市面上没有。这既是定位,也是风险:三条线各自都有人做得比我们深,我们必须靠**三者互相喂**的闭环取胜,而不是任何一条线单独领先。

两件产品侧的事实要记住:

- **安全是路线二的分水岭。** OpenClaw 在 2026-02 被审计出 36% 的 ClawHub skills 含可检测的提示注入,下架近 400 个恶意 skill,公网暴露实例以万计;Cowork 也被演示过用藏在文档里的白字注入让它上传文件。Grok Bot 的做法是:邮件与 webhook 内容一律标不可信,独立模型审查有风险的动作,不可逆的发送先草稿后确认。
- **"最少产品"在落地。** Grok Bot 让用户像对同事一样用聊天交代,"不必先配 workflow、automation 或 routine";纠正会被折进它学到的例行。Claude 的 routines 则是三种触发(定时、webhook、GitHub 事件)叠在一份配置上,云端跑,笔记本合上也在。

## 二、学术:这一年在争的四件事

### 1 自改进:改 harness,不改权重

Lilian Weng 2026-07 的综述把可自改进的东西排成一条梯子:提示 → 结构化上下文 → 工作流 → harness 代码 → 优化器代码;近期的递归自改进更可能走 harness 而不是模型自己改权重。几个结果:

- Darwin Gödel Machine(ICLR 2026):agent 改自己的 Python 代码再在编码任务上测,SWE-bench Verified 20% → 50%,Polyglot 14.2% → 30.7%。"看起来有希望的改动会让 agent 变差甚至不会改代码",所以必须测、不能信。
- Agentic Harness Engineering(复旦/北大,2026-04):瓶颈在**可观测性**而不在能力。给 harness 的每个可编辑部件一个文件级表示,每次改动带变更清单(失败证据、根因、针对性修复、预测影响、约束级别),十轮把 Terminal-Bench 2 从 69.7% 提到 77.0%。但自我归因弱:预测"修了什么"比随机好五倍,预测"会弄坏什么"基本看不见(regression blindness)。
- Hierarchical Self-Improvement(2026-08):任务 harness / 进化器 / 元进化器三层,一个冻结模型;两个上界——反馈保真度(奖励信号不够细就进化不动)和骨干能力(harness 再改也超不过模型本身)。
- Weng 列的坑:弱模型上自改进会变差(STOP 用 GPT-4 涨、弱模型跌);Goodhart(过拟合测试、评审、基准伪影);评估器与权限控制必须在进化循环**之外**且只读;短期优化会牺牲仓库长期健康。

对我们:evolve 已经是 propose–evaluate–accept,评估器在沙箱外、`eval-state` 只读,这两点和文献一致。缺的是观测性——我们只有分数,没有变更清单、没有归因、没有"这次改动预计会动哪些任务"。另外 WorkBuddy 迁移实验落在噪声里(history/evolve-2026-11-01),正是"反馈保真度不够"的表现:任务少、分数粗,进化就没有梯度。

### 2 记忆:看它让下一次做得多好,不看它记得多少

- Memory-Centric Continual Learning(2026-10):外部记忆不该按召回率评,该按**是否改善了后续行为**评;双环——内环更新记忆,外环按任务反馈改管理策略;评测要分开"真学到了"和"只是上下文更长了"。
- Learning on the Job(2026-07):权重冻结,把每一局蒸成自然语言规则存外部记忆;信号只用部署里天然有的东西——一比特的结果,或事后的纠正。τ-bench 银行域上,结果信号把单次成功提到 1.6×,**纠正**提到 2.6×,解了 84 道基线从未解出的题里的 22 道;一个模型攒的记忆能让另一个模型变好。
- AgentCL(2026-06):常规任务流分不出记忆设计的好坏,只有**有依赖的任务流**(后面的题能用前面的知识)才分得出;独立任务上记忆常常让表现退化。
- XSkill / MemSkill 等:Markdown 技能库 + JSON 经验库,带剪枝;记忆操作本身当成可进化的技能。

对我们:001 第 9 阶段的四层记忆是对的形态,但评价标准要改——不是"记了多少",是"同一个用户下一道题好了多少"。用户的纠正是最值钱的学习信号,比做完没做完值钱一倍以上。我们挖任务(mining)目前只挖"合并且带测试的 PR",没有挖"用户纠正了什么"。

### 3 该不该问人:这是能力,不只是礼貌

- HiL-Bench(2026-04):任务里埋了只有做下去才会碰到的障碍(缺信息、歧义、矛盾);指标 Ask-F1 = 问得准 × 障碍找全。前沿模型在"自己决定问不问"时只剩完整信息下成绩的一小部分;三种失败——自信地错、知道不确定但照错、乱问不自纠。用 Ask-F1 做 RL 奖励能学会,且跨域迁移。
- SWE-chat(ICML 2026,6000 个真实 Claude Code 会话):44% 的轮次用户在纠正、报错或打断;agent 很少主动问。agent 写的代码只有 44% 活到用户的提交里。
- Ambig-SWE(ICLR 2026):任务欠规范时,擅自假设比问一句更差。

对我们:006 的"只在做完、卡住、要决定时说话"是输出纪律,但**什么时候算卡住、什么该变成 question 卡**是 harness 的能力,现在完全没有被评测。我们的 question/approval 机制有了,它们的质量没有尺子。

### 4 长时程:用户的真事多数是长的

- Long-Horizon-Terminal-Bench(2026-10):46 个任务,平均一跑 85 分钟、9.9M token、231 个回合;最好的模型 15.2% pass@1,均值 4.3%;稀疏的最终分给不出信号,必须按子任务给部分分。
- SWE-bench Pro:1865 题,人要几小时到几天;Terminal-Bench 到 3.0。

对我们:用户交出去的"把 CI 弄绿""给这段补测试"多数是长时程。评测器现在只有 pass/fail,没有部分分;harness 的 checkpoint 只用于续跑,没有用于评分。

### 5 多 agent:失败在接缝

没有找到专讲编码 agent 团队的论文;从业者共识一致:多数失败不是哪个模型不够聪明,而是 agent 之间的接缝没定义——谁拥有这段对话、什么能传、传回什么、谁能写。"错的 agent 做了写、发、删"是最危险的 bug,看起来像生产力。Grok Bot 的"幕僚长协调专家 + 所有权交接"就是这个问题的产品答案。

## 三、我们该往哪走

不是清单(011 才是清单),是方向与取舍。

**1 把用户的纠正变成学习信号,这是我们独有的闭环。**
路线一没有记忆,路线二不自改进,路线三没有用户。我们三样都有,但现在它们之间只接了一条线:合并的 PR → 任务。应该再接两条:用户的纠正 → 记忆里的规则(Learning on the Job 的 2.6×);用户的纠正 → 该仓库任务集里的一道"依赖题"(AgentCL 说只有这种题能分出记忆好坏)。于是"它记得你"和"它变强"是同一件事的两面:记忆是即时的,进化是批量的,信号都来自这个用户。

**2 评测从"题库"变成"这个用户的历史"。**
通用基准(polyglot、WorkBuddy)留作回归护栏,不再当目标。每个用户一个任务集已经有了(`user-<repo>`),要加三种题:有依赖的题(前面做过的事里有可复用的知识)、埋障碍的题(该问的时候问了吗,Ask-F1)、长题(按子任务给部分分)。进化只在用户的任务集上比较;000 说"评测只回答产品问题",这就是那个问题。

**3 给自改进装上眼睛。**
AHE 的结论直接可用:每个候选版本带变更清单(改了哪个部件、为什么、预计影响哪些任务),评测后对照"预计 vs 实际",归因写进 history。版本回退已经有了,但要能回答"回退到哪一版、为什么"。regression blindness 是已知的,所以回归护栏必须是独立的、进化看不到的集合。

**4 "该问就问"进 harness 的评测。**
question 卡和 approval 卡的质量要有尺子:问了不该问的(打扰)和没问该问的(擅自假设)都要计数。这是 006 规矩的另一半。SWE-chat 的 44% 说明现在的 agent 普遍不问;谁先把这件事做对,谁的 agent 才像同事。

**5 安全是产品而不是合规。**
OpenClaw 的教训是:个人 agent 一旦接进用户的渠道,渠道里来的一切都是攻击面。我们已有的边界(凭证不进沙箱、出口白名单、按动作批准、密钥只在宿主侧)要守住,再加两条:所有从渠道进来的内容(PR 评论、聊天、网页)在 ticket 里标为不可信;不可逆动作(合并、发消息、花预算)的审查由进化循环之外的独立检查做,而不是让 agent 自己判断。

**6 多 agent 先定接缝,再做交接。**
011 第 10 条的"@名字指派"够用一阵。agent 之间传活之前,先把三件事写进 002:谁拥有一段对话、交接时传什么、写动作只能由所有者做。没有这三条的交接是在造事故。

**7 不做的事。**
不训模型(冻结权重 + 外部记忆 + harness 进化已被证明够用,且让"换模型"成为一次更新而不是重做);不做桌面 computer-use(用户是开发者,代码托管和聊天两条渠道先做透);不追公开榜单(Terminal-Bench 之类只当回归护栏);不做 workflow 编辑器(Grok Bot 证明用户不想先配流程)。

## 四、对 001 的影响

这些方向会改 001 的四处措辞,改之前先在这里记下:

- 第 6 阶段"纠正"要加一句:纠正会被记住,并成为以后必须通过的考题。
- 第 9 阶段"它记得你"的衡量从"记得"改为"下一次做得更好"。
- 第 10 阶段"它变强"的更新说明要能回答"改了什么、为什么、在你的哪些事上好了、怎么退回去"。
- 第 5 阶段"被找"要加:它知道什么时候该问你,问得准不准也在被测。

## 五、做完第一轮之后补读的三篇,以及它们改了什么

做 D17 的过程中又读了三份,每一份都直接改了实现(决策 D18 之外的补充):

| 出处 | 它说什么 | 我们改了什么 |
| --- | --- | --- |
| TRACE,《Getting Better at Working With You: Compiling User Corrections into Runtime Enforcement for Coding Agents》([arXiv 2606.13174](https://arxiv.org/abs/2606.13174),2026-06) | 把用户的纠正**存起来再取出来给 agent 看**是建议性的:agent 看了,输出照样可能违反。它把每条纠正改写成原子规则,再**编译成运行时检查,不过检查不许结束**;按作者的数字,留出集上偏好违反率从 100% 降到 37.6%,而加了一个记忆层(Mem0)之后仍有 57.5% 的检查被违反 | 规则多了一条可选的**检查**(路径禁止、新文件命名、新增行禁止,都是确定的正则,模型只写参数不写代码);harness 报 ready 带补丁时宿主先跑检查,违反就把违反处送回去重做,至多两次;之后审查再看一遍(`intake/rulecheck.py`)。真实走一遍时,模型把"别动 eval/ 下面的任何文件"写成了 `path_forbid ^eval/` |
| 《Ask or Assume? Uncertainty-Aware Clarification-Seeking in Coding Agents》([arXiv 2603.26233](https://arxiv.org/abs/2603.26233),Edwards & Schuster,2026) | 把"判断缺不缺信息"从干活的 agent 里拆出来:一个 Intent Agent 看任务状态,缺了就让主 agent 去问;在信息被删掉的 SWE-bench Verified 上 69.4% 对单 agent 加提问工具的 61.2%,简单任务问得更少 | 看门狗在第 10、24 步各读一遍请求与最近八步,只在卡在"只有人能说的东西"上时给 agent 一条来自 host 的输入,要它立刻问(`intake/intent.py`)。第一次真实走"该问就问"时 agent 没问,翻代码翻到步数用完,正是这个病 |
| Claude Code 等的"自动记忆 → 复现两三次再晋升为长期指令"的社区做法(来源多是插件说明与博客,质量参差) | 先捕获到草稿式记忆,重复出现或明显通用的再提升进总是加载的指令文件,要经人批准 | 我们的规则是**立即生效但可当场收回**("记住了 / 忘了这条"),并按仓库隔离;"重复出现才晋升"没有做——规则只有几十条,晋升的收益小于它让用户多做的一次决定 |

另外三个值得记的事实:

- **真实走一遍比任何论文都更快地找出问题**(D18):提示里只写"以 QUESTION: 开头提交"而不写命令,模型就根本提交不出文字——这个协议在单元测试里是好的,在真实里几乎不存在;harness 停在步数上限上不吭声的僵尸单,是真实红 CI 里才出现的。
- **合成的 obstacle 题**可以从用户自己的提交里造(HiL-Bench 的做法,换成用模型从提交里挑出"这次改动定下的一个具体事实",把它从请求里拿掉、变成 blocker),但**必须有"答案在改动里、请求没泄露"的两道检查**;用推理模型出题要给足 token,否则它把预算花在思考上、交回空答案——一开始就是这样,13 个提交只出 2 道,修好后出 7 道。
- **历史是现成的任务集**:一个有几十个提交、带测试的仓库,立刻就有十来道带隐藏测试的题;其中约四成在评测沙箱里收集阶段就错(缺依赖、要 docker 或 Linux 特有调用),被导入检查淘汰,不污染分数。

## 六、上下文要不要缩:两篇相反方向的证据,和我们自己的一次测量

做完压缩候选之后读到的:

| 出处 | 它说什么 |
| --- | --- |
| 《The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management》([arXiv 2508.21433](https://arxiv.org/abs/2508.21433),JetBrains Research,NeurIPS 2025 workshop) | 在 SWE-agent / SWE-bench Verified 上,**把旧的观察(命令输出)遮掉**与用模型做摘要一样有效,且比不处理的 agent 便宜约一半(某模型上便宜 52%、解题率还高 2.6 点);观察占一轮 token 的约 84% |
| 《Token Reduction Is Not Cost Reduction》([arXiv 2607.12161](https://arxiv.org/abs/2607.12161),2026-07) | 在 Claude Code 的配对实验里,最激进的压缩臂把工具输出 token 降了 38.4%,**账单反而涨了 6.8%**:账单的大头是提示缓存的流量,压缩还会让 agent 多走几步;应当按"成功调整后的账单成本"评价,不按 token 数 |

我们的候选(旧输出缩成头尾、最近八条完整)测到的和第二篇一致:每次调用的输入降约四成,步数从 36 涨到 58,每次运行的花费 $0.425 → $0.451,分数在噪声里。两篇不矛盾,差别在于**缓存**:一个滑动窗口式的压缩每走一步就改写前缀,把模型服务商的前缀缓存全部打掉;JetBrains 的设置里没有这个代价(或价目表不分缓存)。所以下一个候选不是"缩得更狠",而是**成批地缩**——上下文超过阈值才一次性缩,缩完到下一个阈值之前前缀保持不变(与 Anthropic 的 context editing 的"到阈值才清理工具结果"同一思路)。这条没有跑:当天模型账户余额用完了。另外我们的价目表把所有输入按未缓存价计费,不反映缓存折扣,评测里的"花费"对长对话是高估的——换成"成功调整后的账单成本"之前,不要用花费的小差异下结论。

## 七、夜里补读的两处(只读到摘要,没有复现)

- **缓存才是账单的主项**。Don't Break the Cache([arXiv 2601.06007](https://arxiv.org/abs/2601.06007))在三家服务商上比较了整段缓存、只缓存系统提示、不缓存动态工具结果:省 40% 到 80%。另有两篇(CliffCompaction [2609.26779](https://arxiv.org/pdf/2609.26779)、TokenPilot [2606.17016](https://arxiv.org/abs/2606.17016))说压缩本身的额外开销很小,但每次压缩都会让前缀缓存作废,所以**压缩的时机**比压缩的方法更要紧。这和我们自己的测量一致:滑动式压缩让每次调用变小,总花费没降(§六)。下一个候选应该是"到阈值才成批压缩一次",而不是每步都改写。这三篇我没有打开原文核对数字,编号较新,引用前请先核实。
- **规则要写进版本里,而不只是记在记忆里**。Self-Improving AI Coding Agents Through Accumulated Behavioral Rules([arXiv 2607.13091](https://arxiv.org/abs/2607.13091))让评审抓到的问题变成可复用的规则,存进受版本管理的指令文件。我们的规则层(纠正 → 规则 → 检查)走的是同一条路,区别是我们的规则带用户的来源和可撤销的"忘了这条";是否要把规则导出成仓库里的指令文件(让其他工具也读到),列为之后的想法,不在这一轮做。

## 八、读了 AHE 和 Self-Harness 的全文之后

AHE([arXiv 2604.25850](https://arxiv.org/abs/2604.25850),附录约四分之一没能读到,抓取工具拒绝返回那一段):

- 七个可变部分(系统提示、工具描述、工具实现、中间件、技能、子 agent 配置、长期记忆)都是固定位置上的文件,一次逻辑改动一个提交;种子只有一个 shell 工具,没有中间件、技能、子 agent。
- 三个同模型的角色:出轨迹的编码 agent、把轨迹提炼成逐任务报告和整体概览的 Debugger、改文件并写改动清单的进化 agent。进化 agent 只能写工作区,日志、验证器、模型配置只读,种子系统提示不可删。
- Terminal-Bench 2(89 题),GPT-5.4,十轮、每题两次、约 32 小时;种子 69.7% → 77.0%。迁移到 SWE-bench-Verified 75.2% → 75.6%,但每次试验少用 12% token。换成 DeepSeek-v4-flash、Qwen、Gemini 也都是正的(+5.1 到 +10.1)。
- 单独加一个部分的消融:记忆 +5.6,工具 +3.3,中间件 +2.2,系统提示 -2.3;加起来 +11.1,全部合起来只有 +7.3,非加性。
- 改动清单自我归因:修好的预测精度 33.7%、召回 51.4%(随机 6.5% 和 10.6%);变差的预测精度 11.8%、召回 11.1%(随机 5.6% 和 5.4%)。作者称之为对回归的盲视。
- 局限:只在 Terminal-Bench 2 上进化;最优配置是按进化集上的 pass@1 选的(迁移集是独立检查);预算和超时是按 GPT-5.4 调的;没有完整的护栏;还没报进化本身的花费。
- 没有的:缓存、人在环里、验证有效性的讨论。

Self-Harness([arXiv 2606.09498](https://arxiv.org/abs/2606.09498)):

- 同一个模型挖失败模式、提 K 个受限改动、在进化集和留出集上都不退步才合并(Δ_in ≥ 0、Δ_ho ≥ 0、其一 > 0)。可改:指令、记忆、子 agent、技能、运行时控制策略、工具,起点是 DeepAgent 配置。
- Terminal-Bench-2.0 的 64 题子集,三个模型,每个候选两次;留出集的提升 +14.2 到 +21.4 个百分点。门是评测系统自动执行的,不是模型。
- 留出集既用来接受又用来报告结果;切出的集很小(一次尝试约 2.4 个百分点);没有消融;没有花费;正文里两处数字不一致。

对我们的位置:没有一篇把评测者放在 agent 之外并三份切分,没有一篇按真实缓存价报花费,没有一篇有用户的纠正或提问质量。这些是我们多出来的,但每一块单独看都小,需要实验来证明,不能靠写。

## 九、同一个人的另外两份材料,读来对照

**all-products 仓库**(固件开发智能体加主动录音卡):

- 产品一的定位是"包在编程智能体外面、让它把 ESP32 固件做对的那一层 harness":环境(每人一台云沙箱、固定 IDF 版本、预热的编译缓存)、工具(模拟器截图与按键)、知识(官方 > 本沙箱实测 > 社区三层)、验收与证据(截图断言、内存基线、可能假通过的提示)、真机闭环、成本与运行、固定的测评任务。智能体可以换。
- 它们的做法里和我们最相关的三条:开发和线上用不同的模型账户(余额告警,402 要显示成用户看得懂的状态);任何验收断言先在故意弄坏的条件下跑一次;改指引先做 A/B(同一提示、同一构建、每组至少三次)。指标的先后是复用质量、验收可信、人审负担、成本、速度。
- 文档的规矩:状态标签(已定、提议)、每个数字附出处、不写没验证的话、20 多条决定逐条记理由与放弃了什么。这和我们的 decisions.md 同一个路子。

**"AI 辅助研发与工程智能化平台"总体架构 V0.3**(军用 OpenHarmony 研发体系,评审稿):

- Octop 做总编排和人的入口,DSH、DevEco Code、DevEco CLI、Codex、Claude Code 是专家;Kaneo、Gitea、Gitea Actions、专用缺陷库各管自己的数据;CubeSandbox 隔离执行不可信代码;硬件设备农场做真机测试。
- 硬约束:数据不出内网、模型全部内部部署并按密级路由、默认只读、合并发布关闭缺陷必须人签字、每个 Skill 上线前过评测、全程可追溯。三级权限:只读建议、生成草稿、受控执行。
- Harness 章节的六个组件(数据集、沙箱、回放、打分、红队、评分卡)、三道上线门(离线达标、灰度二到四周、试点确认)。
- 我们已有:微虚拟机沙箱、评测器与接受门、批准与独立审查、轨迹可回放。我们没有:数据分级与内部模型路由、红队评测、Skill 体系、统一身份。
- 文档里没有 harness 自我进化这一层;这是我们的差异点,也是接进这类平台时最容易被问到的:改动由谁批准、凭什么证据。

## 出处

产品:
- 云端编码 agent 对比:[Incredibuild](https://www.incredibuild.com/blog/best-github-coding-agents-2026)、[devtoollab](https://devtoollab.com/blog/best-background-coding-agents)、[techsy](https://techsy.io/en/blog/background-coding-agents-compared)
- Grok Bot:[VentureBeat](https://venturebeat.com/ai/spacexais-grok-bot-turns-agents-into-persistent-digital-coworkers-that-can-operate-your-apps-for-120-per-month/)、[Composio 指南](https://composio.dev/blog/guide-to-frok-bot)、[codersera](https://codersera.com/blog/grok-bot-complete-guide-2026/)
- OpenClaw 与安全:[Tencent Cloud 词条](https://www.tencentcloud.com/techpedia/141605)、[vida.io 对比](https://vida.io/blog/openclaw-vs-claude-cowork-vs-openai-operator)、[Penligent 分析](https://www.penligent.ai/hackinglabs/?p=2217)(安全厂商,措辞可能夸大)
- Claude routines / scheduled tasks:[hatchworks](https://hatchworks.com/?p=35376)、[memorylake](https://www.memorylake.ai/en/blogs/claude-code-routines-carry-over)
- autoresearch:[NYU Shanghai RITS](https://rits.shanghai.nyu.edu/ai/karpathy-open-sources-autoresearch-100-ai-experiments-overnight-on-one-gpu/)、[DataCamp](https://www.datacamp.com/tutorial/guide-to-autoresearch)

学术:
- Lilian Weng,[Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/)(2026-07)
- Darwin Gödel Machine,[arXiv 2505.22954](https://arxiv.org/abs/2505.22954)(ICLR 2026)
- Agentic Harness Engineering,[arXiv 2604.25850](https://arxiv.org/abs/2604.25850)
- Hierarchical Self-Improvement,[arXiv 2608.08466](https://arxiv.org/abs/2608.08466)
- Self-Improvements in Modern Agentic Systems: A Survey,[arXiv 2607.13104](https://arxiv.org/abs/2607.13104)
- Memory-Centric Continual Learning,[arXiv 2610.29357](https://arxiv.org/abs/2610.29357)
- Learning on the Job,[arXiv 2607.22157](https://arxiv.org/abs/2607.22157)
- AgentCL,[arXiv 2606.02461](https://arxiv.org/abs/2606.02461)
- XSkill,[arXiv 2603.12056](https://arxiv.org/html/2603.12056v1);From Storage to Experience(综述),[arXiv 2605.06716](https://arxiv.org/pdf/2605.06716)
- HiL-Bench,[arXiv 2604.09408](https://arxiv.org/abs/2604.09408)
- SWE-chat,[arXiv 2604.20779](https://arxiv.org/abs/2604.20779)(ICML 2026)
- Long-Horizon-Terminal-Bench,[arXiv 2607.08964](https://arxiv.org/abs/2607.08964)
- 上下文:The Complexity Trap [arXiv 2508.21433](https://arxiv.org/abs/2508.21433);Token Reduction Is Not Cost Reduction [arXiv 2607.12161](https://arxiv.org/abs/2607.12161)
- TRACE(用户纠正编译成运行时检查),[arXiv 2606.13174](https://arxiv.org/abs/2606.13174);Ask or Assume,[arXiv 2603.26233](https://arxiv.org/abs/2603.26233)
- 多 agent 接缝(从业者):[urandom.io](https://urandom.io/blog/2026-03-17-multi-agent-systems-fail-at-the-handoffs)
