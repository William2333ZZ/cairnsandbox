# 文档

一个让 agent 在隔离沙箱里运行、并在可信评测下让它的运行系统(Cairn)自我改进的平台。代码在 [`prototype/fc-node/`](../),接口逐字约定在 [`CONTRACT.md`](../CONTRACT.md)。

## 按顺序读

每篇只建立在它前面的文档之上,读者不会遇到没定义过的词。每篇末尾的"尚未实现"说明正文里哪些还是规格。

| 编号 | 文档 | 回答什么 |
| --- | --- | --- |
| 000 | [原则](000-principles.md) | 判断一切的那条原则,以及它对系统、设计、操作者的要求 |
| 001 | [用户使用过程](001-journey.md) | 产品是什么:用户怎样得到 agent、交代、走开、被找、教它例行、看它变强;不分期 |
| 002 | [领域模型](002-domain.md) | 概念、它们守的规则、否决的方案、术语表 |
| 003 | [沙箱与平台](003-sandbox.md) | 一台沙箱在宿主侧经历什么:状态、规格、层、网络、节点、控制面 |
| 004 | [沙箱内系统](004-guest.md) | 五层、运行时与 harness 的边界、内核 ABI、Landlock、harness 的可变部分、晋升路径 |
| 005 | [Agent 服务](005-intake.md) | 对话与路由、接入、Trigger 怎样变成 Ticket、Mandate 能走多远、例行、批准、记忆 |
| 006 | [执行](006-execution.md) | 派工到沙箱、Report、Input、交付、Watchdog、恢复 |
| 007 | [评测与晋升](007-evaluation.md) | 任务来源、Run、噪声、级联、接受、档案库、晋升 |
| 008 | [工作台](008-workbench.md) | 对话首页、需要我、一件事、管理页;本地站点 |
| 009 | [安全与数据](009-security-and-data.md) | 上限在哪强制、威胁与对策、凭证、数据归属、保留与删除 |
| 010 | [部署与运维](010-deployment.md) | 服务、密钥、构建、升级、CI、排障 |
| 011 | [实现架构](011-architecture.md) | 代码索引:模块、协议、状态目录、测试 |
| 012 | [扩展手册](012-cookbook.md) | 加层、加任务集、加 ABI 操作、加 harness 语言、加 Source、加纠正入口、加规则检查、加审查规则、加一种任务、让 agent 能被交接 |
- [013 威胁模型](013-threat-model.md):资产、信任边界、五类攻击者、每条边界的控制与已知未做、残余风险、外人该看哪里

## 用的人看

- [guide/](guide/README.md):租户手册、按端点的 API 参考、Python SDK 与 MCP 参考、四篇教程(D49)。

## 其余

- [`decisions.md`](decisions.md)([索引表](decisions-index.md)):设计决策记录,只追加;每条写结论、理由、可推翻条件。编号文档是现行设计,决策记录是为什么会这样。
- [`research/`](research/):文献与同类系统的调研。[沙箱云控制台重新设计方案](research/console-redesign-2026-11.md)(待确认)、[自改进算法综述](research/rsi-algorithms-survey.md)、[DSec 对照](research/dsec-vs-ours.md)、[DeepSeek Super-agent 笔记](research/dsh-superagent-notes.md)、[Always-on agent 的使用过程(CS146S / Grok Bot)](research/always-on-agents.md)。[2026-11 的产品与学术现状及方向](research/landscape-2026-11.md)(决策 D17 的依据)。[CubeSandbox 对照](research/cubesandbox-vs-ours.md)(腾讯云的 E2B 兼容沙箱云,与我们沙箱层的逐项比较)。
- [`history/`](history/):按日期记录的原型实验手记与数字,[prototype-firecracker.md](history/prototype-firecracker.md)。[d17-real-walks-2026-11-02.md](history/d17-real-walks-2026-11-02.md):D17 做完后在开发机上用真实模型走一遍、第一次在用户自己的仓库上评测,以及发现的洞。
- [`archive/`](archive/):被编号文档取代的旧版设计。

## 写文档的规矩

- 现在时,写系统是什么,不写它什么时候变成这样;历史进 `history/`,原因进 `decisions.md`。
- 表格的分类要可检验、互斥、穷尽;"第一个匹配的行生效"要写明。
- 一个概念只在一篇里定义,其余篇用链接。
- 数字只在改变读者要做的事时出现。
- 否决过的方案写进 002 的否决表或 decisions,不留在正文里当备选。
