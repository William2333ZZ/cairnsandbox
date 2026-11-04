# 004 沙箱内系统

本文描述一台沙箱里面从内核到 harness 的分层,以及 harness 与运行时之间的边界。它建立在 [002 领域模型](002-domain.md) 与 [003 沙箱与平台](003-sandbox.md) 之上。

## 五层

按"谁拥有、谁版本化"分层。每层是配方加内容寻址的产物加父哈希,叠放顺序固定;可写层在最上面。

| 层 | 内容 | 属主与可变性 | 位置 |
| --- | --- | --- | --- |
| 内核层 | 裁剪的 Linux 6.18:从 Firecracker 官方 microvm 配置做减法,去模块、io_uring、user namespace、BPF、netfilter、IPv6、NFS、输入设备、休眠、其余 LSM;LSM 只留 landlock、lockdown、yama;补 EROFS | 平台,冻结 | vmlinux |
| 系统层 | Debian 13 essential:libc、coreutils、bash、util-linux、e2fsprogs、iproute2、curl、tar;三个用户 root / aios / agent;没有解释器,没有 systemd | 平台,冻结 | `/`、`/usr` |
| agent-runtime | 一个 Rust 静态二进制 `sbx-runtime`:sandboxd(vsock 协议)+ 内核 ABI + Landlock 下放 + 从 harness 晋升进来的组件 | 平台与晋升组件;R3,只人改 | `/usr/local/lib/sandboxd`,以 aios 用户运行 |
| agent-langs | 可重定位的 CPython 3.13 加 jinja2,bun 单文件;两者同在;所有 harness 版本共享 | 平台构建;换解释器版本是 R3 | `/aios/runtime/{python,bun}`,`/aios/bin/{python3,bun}` |
| agent-harness | harness 代码与 7 个可变部分,只有代码;默认 harness 是 Python(`/aios/bin/aios`),TypeScript harness 走 `/aios/bin/aios-ts`,可并存 | **RSI 改的层**:R1 文本,R2 代码 | `/aios/harness/`,以 agent 用户运行 |
| env | 任务或项目需要的工具链:gcc、git、make、pytest、jq、系统 python | 任务集导入时构建,随任务集版本固定 | `/usr`、`/opt` |
| 可写层 | 工作区 | agent | overlay 上层,大小由 `disk` 决定 |

**Cairn 版本 = agent-runtime 哈希 + agent-langs 层 + agent-harness 层。** 前两者很少变;一个候选只重建几十 KB 的 harness 层。

## 规则

- 隔离是文件系统与内核的事实:runtime 以 aios 用户运行,代码只读;harness 以 agent 用户运行,写不到 `/aios`、`/usr`,只能经 ABI socket 产生受控的副作用。
- harness 层只放代码,所以很小;解释器在 langs 层。
- 回滚 = 换回旧的层哈希组合;升级 = 新层加编号迁移脚本,迁移脚本是候选改动的一部分,要进评测。
- 每次构建后自检:能编辑代码、能调模型、能写事件;不过的层不进评测。
- 层是制品:代码改了就得重建,否则沙箱里跑的还是旧的。

## 运行时与 harness 的分工

| | agent-runtime(Rust) | agent-harness(Python 或 TypeScript) |
| --- | --- | --- |
| 身份 | aios 用户,代码只读 | agent 用户,可写工作区 |
| 职责 | 执行原语:exec、PTY 会话、文件、pack、校时、网络配置、冻结;能力检查、本地预算计数、只追加事件日志、检查点钩子、进程组与超时;给 harness 进程下放 Landlock | 策略:agent 循环、提示、工具语义、技能、记忆、子 agent 编排、中间件 |
| 可变性 | 冻结,人写 | RSI 改的全部在这里 |
| 接口 | unix socket 上的 NDJSON,SO_PEERCRED 认 uid,ABI 版本化 | 只能通过这条 socket 产生副作用 |

一句话:runtime 提供"能做什么",harness 决定"怎么做"。

## 内核 ABI

socket `/run/aios/abi.sock`,NDJSON,一行一请求;只认 root、aios、agent 三个 uid。

| 操作 | 方向 | 内容 |
| --- | --- | --- |
| hello | harness → runtime | 返回 ABI 版本、能力表、调用方 uid、当前预算 |
| event | harness → runtime | `{type, data}`,追加到事件日志,返回序号 |
| budget_get / budget_charge | harness → runtime | 查询;按 token 与费用扣本地预算,超限返回 `over` |
| checkpoint | harness → runtime | 记一个带说明的检查点事件 |
| report | harness → runtime | `{state, text?, attention?, deliverable?}`,六态之一;写入事件流。`attention` 是 `{kind: question, question}` 或 `{kind: approval, action, draft}`(005"批准");只有终态与带 attention 的 waiting 会变成对用户说的话(006) |
| event type `note` | harness → runtime | `event{type:"note", data:{text}}`:这件事的工作笔记(记忆的 notes 层),宿主在派工结束时从事件流收走;harness 读不回别件事的 notes。用的是已有的 event 操作,不是新操作 |
| inbox | harness → runtime | `{since}`,返回宿主经 `abi_input` 追加到 `/run/aios/inbox.jsonl` 的 Input;harness 每步读一次 |
| ticket_get | harness → runtime | 宿主经 `ticket_set` 写入 `/run/aios/ticket.json` 的 Ticket:文本、范围、分支、基线提交、命中规则正文、此前的输入、`memory` 段(profile 事实、相关 log、同仓库的 notes;标为信息) |

事件日志 `/run/aios/events.jsonl` 由 root 持有,agent 只能经 ABI 追加,读不到;宿主经 sandboxd 读,节点以 `GET /sandboxes/<id>/events` 暴露;本地预算上限以 `PUT /sandboxes/<id>/budget` 下发。模型代理侧的上限仍是权威,本地计数只用来让 harness 早停。

ABI 版本化;harness 层声明所需 ABI 版本,不兼容的 runtime 层不能叠。ABI 的变更是 R3。

## Landlock 下放

exec 可带 `landlock` 选项:`true` 是默认 harness 约束,只读全盘;可写 cwd、`/tmp`、home、`/dev/null`;TCP 只许连出口代理端口;ABI ≥ 6 时附加 unix 抽象 socket 与信号作用域。也可显式给 `{ro, rw, connect_ports}`。规则在子进程 setsid 之后、降权之前施加,先 `PR_SET_NO_NEW_PRIVS` 再 `landlock_restrict_self`。非目录路径只接受文件类权限位;显式策略始终放行 `/dev/null`。评测器对 agent 步骤默认开启,验证步骤不需要。

## harness 层的组件

可变层分成互相独立的文件级部分,每个单独 diff、审、回滚。分法对齐 Agentic Harness Engineering([arXiv 2604.25850](https://arxiv.org/abs/2604.25850))的七部分,外加一个兜底;早先的 8 个组件(借 Harness²)里,护栏和记忆的归属改了,理由在表后。

| 组件 | 种子里是什么 | 谁能改 |
| --- | --- | --- |
| system_prompt | `config.json` 的 `system_template`、`instance_template` | 元 agent、人 |
| tool_description | `model.py` 的 `BASH_TOOL` | 元 agent、人 |
| tool_impl | `env.py`、`model.py` 的观察模板 | 元 agent、人 |
| middleware | `middleware.py` 里的类,在 `config.json` 的 `agent.middleware` 里按名字开关;钩子 `before_step`、`before_query`、`after_step`;候选可以用 `{"class": "包.模块:类"}` 加自己的类 | 元 agent、人 |
| skill | 个人 harness 目录的 `skills/<name>/SKILL.md`(见下节);任务里列名字与说明,agent 用时自己读 | 人;元 agent 经 proposals/ |
| subagent | 种子里没有;用户层面的"交接"在宿主,不在 harness | 暂无 |
| memory | `aios/memory.md`:harness 自己带进每个任务的笔记,种子是空的;个人 harness 目录里的 `memory.md` 替换它 | 元 agent、人 |

**不在可变层里的两样**:护栏(Landlock、预算、独立审查、完成关)在 agent 够不到的地方,把它放进 agent 能改的文件等于让被约束者改约束,评测器的篡改检查也在拦这类改动;用户的记忆(规则、画像、日志)归宿主,由宿主在派工时放进任务,agent 只读。`memory.md` 是另一回事:它是 harness 自己的经验,不是关于这个用户的事实。

## 个人 harness 目录

上表是 harness **代码**里的可变部分,随层一起构建,改一处要重建层、过评测。一个人日常要改的东西(规则、笔记、提示的措辞、几条技能、一两个中间件)不该走这条路:它们属于这个人,不属于某个 harness 版本。所以 Cairn 在代码之外再读一个目录——个人 harness 目录(`aios/home.py`,格式在 CONTRACT §5a):

```
<home>/                 一个人一个;建议是他自己的 git 仓库,换机器带走
  config.json           覆盖 config.json(字典逐层合并)
  memory.md             harness 的笔记,替换包内的
  rules/*.md            这个人的长期规则,一条一个文件
  prompts/*.md          system / instance 两个模板;communication / scope / delivery 三段团队提示
  skills/<name>/SKILL.md
  middleware/*.py       自己的钩子类,在 config.json 里按 {"class": "模块:类"} 引用
  tasks/<repo>/...      回归题,归评测器;运行时不读
  proposals/<id>/       机器的提案,同样的布局,外加 PROPOSAL.md(为什么)与 DELETE(要删的路径,一行一个);运行时不读。evolve 接受一个候选就写一个(EVOLVE_HOME)
  history.jsonl         每次 apply 一行
```

三条规矩:

1. **人改立刻生效,机器改要过门。** 人在目录里改什么,下一次运行就用什么,没有审批;机器(元 agent、候选 harness)只能往 `proposals/<id>/` 写,`python -m aios.home apply <id>` 才搬进来,而搬进来的前提是评测的接受门过了(007 评测)。这是把"谁能改"从文件权限变成目录位置:同一份格式,两个入口。
2. **目录坏了不开跑。** `check` 列出问题(instance 模板没有 `{{task}}`、中间件类找不到、config 多了不认识的键……),运行时在调模型之前退出码 1。一个声称有某个行为而实际没加载的 harness,比没有这个行为更糟。
3. **版本是内容摘要。** 运行时读的文件的 sha256 前 10 位(`h-…`)写进每条轨迹的 `info.config.agent.harness_digest`;一次接受绑定的是三元组(sandboxd 版本、目录摘要、Cairn 版本),换了哪个都得重评。

优先级:工单里宿主放的规则在前(那是这个用户纠正过的事,见 005 接单层的"纠正"),目录里的规则在后,任务正文始终最高;团队提示段落工单自带的优先。目录与宿主的记忆是两套:宿主的记忆是关于这个用户的事实,由宿主放进工单;目录是这个人对 harness 的设置。宿主怎样把目录送进沙箱(挂载还是 ticket_set 一并下发)是 006 执行层的事,还没定。

**缓存不变量**:服务商缓存的是发给模型的最长相同前缀,命中的输入只收未命中的 1/50(D22)。所以 `before_query` 里每一步都改写早先对话(滑动窗口)等于让整段对话重新按全价算;要改就成批地改(`batch`),或不改。内置的 `compact_history` 带 `batch` 参数。

中间件是这套里证据最强的一块:AHE 的消融里单加工具、中间件、长期记忆各自都涨,只改系统提示反而降;我们自己的 11 条基线轨迹里,最大的毛病(读很久才写、压缩打断缓存)也都是循环里的钩子。

## harness 的最小义务

- 模型调用进行中每秒读一次 inbox;人的话一到就放弃该次调用、重新规划(docs/006 插话)。
- 启动时读 Ticket 或任务文件;经 `hello` 取预算与能力。
- 每次模型调用后 `budget_charge` 并记 `model_call` 事件;超限以退出码 2 结束。
- 循环里每轮看一眼 inbox。
- 结束前发一次 `report`;退出码 0 = 提交,2 = 预算或步数用尽,3 = 格式错误过多,1 = 异常。
- 轨迹写到 `--out` 指定的文件,格式与评测器约定一致。
- ABI socket 不存在时所有调用都是空操作,harness 在任何环境都能跑。

## 晋升路径:Python 用来演化,Rust 用来冻结

1. harness 里某个组件在档案库里连续若干版本不再变化、评测稳定,成为冻结候选。
2. 人把它重写成 Rust,放进 agent-runtime,经 ABI 暴露为一个能力;harness 改为调用该能力。
3. 新 runtime 层加去掉该组件的 harness 层作为新 Cairn 版本走评测,不退步才晋升。
4. 长期 runtime 变厚、harness 变薄。

晋升进 runtime 的东西从此只能人改。

## 尚未实现

TypeScript harness 本体(`aios-ts` 启动器是占位);子 agent 组件与技能目录(种子里还没有);检查点的内容(目前只记事件)。ABI v2 已含 `report`、`inbox`、`ticket_get`。
