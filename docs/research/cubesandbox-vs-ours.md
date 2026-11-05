# CubeSandbox 对照(2026-11-03)

读的是 [tencentcloud/CubeSandbox](https://github.com/tencentcloud/CubeSandbox) 主分支 668224e(2026-11-02),v0.7 之后;看了架构概览、快照 / 网络两篇深潜、安全代理、生命周期、模板、MCP 网关、PVM 部署、路线图,以及 guest agent 与 cubecow 的 README。对照的是我们的 [003 沙箱与平台](../003-sandbox.md)、[004 guest](../004-guest.md)。

> 第一版把我们写成"一个人的平台、单机原型阶段不需要控制面与调度",用户当天纠正:**我们也是云产品,沙箱是必争之地**。控制面、节点内调度、E2B 兼容不是"它有我们不用补"的,而是要补齐的。本文按这个前提改过;第一版的判断保留在"改过的判断"一节,免得再犯。

## 一句话

两者都是**沙箱云**。CubeSandbox 今天是一个只做执行面的沙箱云:E2B 兼容 API,毫秒级创建、千级密度、企业网络安全,沙箱里跑什么它不管,agent 是别人的。我们是一个**执行面与 agent 面一起做**的沙箱云:沙箱里有 sandboxd 的内核 ABI、预算、事件日志、工单与 inbox,外面有 harness、评测与进化。在"微虚拟机 + 快照 + 出口代理 + 控制面 + 调度"这一段我们要做到和它同一水平,这是入场券;在"沙箱知道里面跑的是 agent"这一段它没有,这是差异。

## 逐项对照

| 维度 | CubeSandbox | 我们(fc-node) | 差距与判断 |
| --- | --- | --- | --- |
| VMM | 自己维护的 Cloud Hypervisor 分支(RustVMM,`hypervisor/`),支持 PVM(无 /dev/kvm 的云主机上跑) | Firecracker 1.17 + jailer,容器内运行 | 它的 VMM 可改(加 SysCtrl 端口、pmem 注入 agent);我们用上游二进制,改不了但省维护。**PVM 值得记住**:开发机不给 kvm 组时它是另一条路,但要换宿主内核(OpenCloudOS),共享机器上做不到 |
| guest 内守护 | cube-agent:Kata agent 改的,PID 1,ttrpc over vsock,**在 VM 里再起 OCI 容器**(rustjail);SDK 的命令 / 文件走沙箱内另一个 `envd`(E2B 的) | sandboxd:自己写的 Rust 单二进制,vsock NDJSON,直接 exec / PTY / 文件 / pack,**没有容器层**;另有内核 ABI socket 给 harness | 它们是"VM 里跑容器"的 Kata 模型,镜像来自 OCI;我们是"VM 就是环境",层来自配方。它的好处是任何 Docker 镜像直接变模板;我们的好处是没有第二层隔离的开销与复杂度,并且 sandboxd 知道 agent 的事(预算、事件、report、ticket、inbox)——**这部分它完全没有**,也不打算有(路线图全是基础设施) |
| 根文件系统 | 模板 = OCI 镜像 → Buildkit → rootfs + 冷启动到探针 2xx → 内存快照;rootfs 卷在 XFS 上 FICLONE 克隆 | 只读基础镜像 + 只读层(配方 pack 出来)+ 可写 ext4,guest 内 overlayfs;层经 virtio-pmem + DAX 共享页缓存 | 它靠文件系统 reflink 做 O(1) 克隆,我们靠层共享 + 可写层新建。两者都不复制数据。它要求宿主有 XFS reflink 分区;我们没有这个要求。**它的"从运行中沙箱提交为模板"= 我们的 pack** |
| 内存快照 | 三种模式:Full / Incremental(pagemap 匿名页)/ SoftDirty(真增量);恢复用 `mmap(MAP_PRIVATE)` 映射镜像文件按需缺页 | Diff 快照链(memchain,只写改过的页),恢复用合并文件或 userfaultfd 按需供页 | 同一个问题的两种解法。它的 Incremental 用"匿名页 ≡ 启动后写过的页"这个等价关系,不用 dirty tracking,很漂亮;我们的链合并在 JAIL=1 下是必须的(uffd 需要非特权 userfaultfd)。**可借**:pagemap bit 61 判匿名页比我们现在的 Diff 链少一次合并;但 Firecracker 的 Diff 快照本身已是增量,收益要量 |
| 快照 / 克隆 / 回滚 API | `POST /snapshots`、`/fork`、`/rollback`(原地回滚,不重建)、从快照创建沙箱(preview);跨节点 Pause/Resume 经 S3 / JuiceFS | fork(n 台,写时复制)、暂停 / 恢复、bundle 导出 / 导入、迁移、evacuate | **我们缺原地 rollback**:现在"回到某一步"只能 fork 出新沙箱,工单的沙箱 id 会变。对评测器无所谓(ephemeral 重跑),对工作台的长会话有用(agent 试错一轮失败,回到检查点继续,inbox 与 ticket 不变)。它的检查点语义正是我们 ABI 里 `checkpoint` 一直只记事件、没落实的那半边 |
| 网络数据面 | CubeVS:三个 eBPF 程序做 SNAT / 策略 / ARP 代理,per-TAP LPM trie,TAP 预建池(省 59 ms);DNS 报文层的域名放行 | 每 VM tap + nftables,guest 内无 DNS,出站全经出口代理按域名放行 | 它是千级并发下的内核态方案;我们单机几十台,nftables 够用。它的"域名策略靠拦 DNS 查询"有已知绕过(hosts 文件),我们"guest 内没有 DNS、只能走代理"更紧,但代价是沙箱里 `pip install` 必须经代理 |
| 出口 L7 代理 | CubeEgress:OpenResty TPROXY,**MITM 动态签证书**(根 CA 烧进模板),按 SNI / host / path / method 规则 first-match,**凭证注入**(密钥不进沙箱),JSONL 审计,WebSocket 透传 | 出口代理按域名放行;**模型代理**单独一层:每沙箱令牌 + 预算 + 价格表 + 缓存计价,上游密钥不进沙箱 | 同一思想("密钥在代理,不在沙箱")做在不同层。它是通用的 HTTP 凭证注入,我们只对模型 API 做了,但多做了**计量与预算**(它没有任何按沙箱算钱的东西)。我们的 `audited` 网络模式还没实现,它的 CubeEgress 就是 audited 模式的参考实现 |
| 生命周期 | running / pausing / paused / resuming / terminated;空闲超时 → kill 或 pause;**自动恢复**:请求到达暂停的沙箱,cube-lifecycle-manager 透明唤醒;paused 不占 CPU 内存 | creating / running / paused / unreachable / failed / deleted;空闲超时暂停,任何请求透明恢复;durable 暂停后发 bundle | 基本一致。它多了 `unknown` 态与"删除 paused 不先唤醒";我们多了 unreachable + evacuate。它的自动暂停是产品卖点(密度),我们是为长会话 |
| 控制面 | CubeAPI(Rust)+ CubeMaster(Go)无状态,Redis 是唯一可信源;CubeOps 运维分离,多副本;K8s chart、Terraform 一键;WebUI 管沙箱 / 模板 / 节点 / 版本矩阵 | fc-cp 单实例,路由落盘,其余从节点对账重建;节点代理是运行态权威;工作台 | **要补**:多副本无状态控制面。我们"节点是权威、控制面对账"的取向可以保留(它也是节点本地数据面),缺的是副本间的共享状态(路由表、沙箱元数据、生命周期事件流、自动恢复的分布式锁)。它选 Redis;我们 003 的"尚未实现"里写着多实例控制面,现在该排进去。它的 CubeOps 把节点运维(排空、均衡、版本矩阵)从调度里拆出来,也该学 |
| 节点内调度 | Cubelet(Go)+ containerd + Shim v2;TAP 预建池(省 59 ms);资源感知放置;路线图上有亲和 / 反亲和、优先级类、在线均衡、带迁移的排空 | fc-node:准入(`MAX_VMS`、`MEM_BUDGET_MIB`)、power-of-two 放置、模板键命中快照直启、`TEMPLATE_POOL` 热池、SCHED_IDLE QoS、排空时暂停到盘 | **要补**:TAP 池、按资源(内存 / CPU / 已有模板快照 / 热池)的放置打分、优先级类(交互 > 评测 > 后台进化)、在线均衡与带迁移的排空。我们已有它没有的 QoS 分级与 ephemeral / persistent 两类;这两样在云产品里就是"交互沙箱"与"批处理沙箱"两种 SKU,要保留 |
| 存储卷 | Volume 框架(E2B 兼容,插件后端,S3 卷,跨沙箱共享,独立生命周期);跨节点暂停恢复经 S3 / JuiceFS | 没有独立卷;工作区层(pack)跟着工单;bundle 经 fc-store | **要补**:E2B 形状的卷。我们的"工作区 = 工单上次派工打包的层"是个窄解;harness 目录、仓库缓存、用户数据都该是卷。**和 D29 直接相关**:现在每次派工 tar 一遍,该变成一个只读卷。003 里"对象存储作为分块仓库后端"也在这一项里 |
| 对外接口 | **E2B 兼容 API + SDK**(命令、文件、PTY、端口转发、暂停恢复、快照、卷、MCP);换一个环境变量就从 E2B 云切过来;OpenClaw / DSH 经 Skill 或插件把它当执行面 | 自己的 HTTP(CONTRACT §1、§2)+ 内核 ABI;没有 SDK | **要补**:E2B 兼容这一层。它证明了这层很薄(CubeAPI 只是翻译,E2B 的形状就是命令 / 文件 / PTY / 端口 / 生命周期),但它决定了外部 agent(DSH、Codex、OpenClaw、别人的 harness)能不能零改动接进来——沙箱是必争之地,争的就是谁的沙箱被这些 agent 默认选用。我们的 ABI 不是替代,是在 E2B 形状之外多给的一层:**用 E2B 接口进来的 agent 得到和别人一样的执行面;用我们 harness 进来的 agent 多得到预算、工单、inbox、评测与进化**。两层并存,不互相要求 |
| 评测与进化 | 无 | 评测器(任务集、lift 计分、对照臂)、evolve、接受门、版本与回退 | 它不做。这是我们在执行面之上的差异化,但只有在执行面不输的前提下才算优势 |
| 性能(它自己报的) | 创建 < 60 ms(预快照 + 资源池),单沙箱开销 < 5 MB,单机数千实例 | 冷启动 0.40–0.56 s,快照启动 0.08 s;内存用 pmem/DAX 省 71% | 它的 60 ms 是"从模板快照恢复 + TAP 池 + 预分配";我们快照启动 80 ms 同量级。**要做同机对比**:创建时延、每沙箱内存开销、单机密度三个数,用它的 benchmark 方法在我们的开发机上各跑一遍;没有这三个数就谈不上"必争" |

## 那篇"地基战"文章说了什么(知乎《腾讯云 Agent Runtime 往下挖,是一场 microVM 地基战》,云计算指北,2026-07-15)

用户让我读的。它把腾讯 AGS、AWS(Firecracker / Lambda MicroVMs / AgentCore)、阿里(Kata → Dragonball → RunD / AgentRun)摆到一张表上,结论有四条和我们直接相关:

1. **发动机在收敛,整车才是差距。** 三家的 VMM 都是 Rust、都在 rust-vmm 谱系里(Firecracker、Dragonball、Cube)。拉开距离的是"开源开了多少、生产磨了多久"。它的原话:把 Firecracker clone 下来,离能上生产的平台还差最难的 90%——**调度、快照恢复、资源池化、网络、存储、多租户、计费、规模化安全加固**。这九样就是我们沙箱层的对标清单;我们用的正是 Firecracker 这台发动机,整车做到哪一步要按这九样逐项说。
2. **AGS 是地基不是平台:有手脚,没大脑。** 文章说腾讯公开文档里没有 Agent Loop、模型接入、编排;循环由用户自己的代码或另一条产品线承担。这和我们的 ABI 对照一致:我们的"大脑"(CairnSandbox runtime、harness、评测与进化)是它没有的那层。但文章也提醒:阿里 AgentRun、AWS AgentCore **都托管循环**。也就是说"地基 + 大脑"在三家里有两家在做,我们不是独一份,只是我们的大脑会自己改(进化)。
3. **E2B 兼容是国内两家的共同选择,AWS 不兼容。** 腾讯、阿里兼容 E2B 以承接现有生态,AWS 自建接口以定义边界。文章把 E2B 称为"事实标准"。我们要承接外部 agent,走 E2B 兼容这条路没有悬念;AWS 那条路要有 Lambda 的生态才走得通。
4. **商用成熟度的信号:GA、SLA、计费口径、第三方横评。** 腾讯内测近一年没 GA,文章归因于全栈自研太重(OS → 内核 PVM → 虚拟化 Cube)。对我们的提醒:**沙箱层每一项都要有可公开的数字与可复现的 benchmark**(它引阿里 RunD 的 ATC'22 数字:88 ms 启动、单机 2500+、每个 < 20 MB;腾讯 65 ms),没有数字就没有"必争"的资格。

把它的"90%"清单对到我们现状(003):

| 文章的九样 | 我们有 | 缺 |
| --- | --- | --- |
| 调度 | 准入、power-of-two、模板键、热池、QoS | 资源打分、优先级类、均衡、带迁移的排空 |
| 快照恢复 | Diff 链、uffd、模板快照、bundle、暂停 / 恢复、fork、迁移、evacuate | 原地 rollback、跨节点恢复的性能数字 |
| 资源池化 | `TEMPLATE_POOL` 热沙箱池 | TAP 池、内存预分配 |
| 网络 | 每 VM tap + nftables、出口代理按域名、closed / allowlist | eBPF 数据面(千级并发才需要)、audited 模式、L7 规则与通用凭证注入 |
| 存储 | 内容寻址分块仓库 fc-store、层、lazyfs、pmem/DAX | 卷(E2B 形状)、对象存储后端 |
| 多租户 | 控制面用户与令牌、Agent 服务按用户隔离 | 多副本控制面、配额、组织 / 项目层级 |
| 计费 | 模型代理按沙箱计量、预算、缓存计价、每日上限 | 沙箱本身(CPU 秒、内存、存储)的计量与账单 |
| 规模化安全加固 | jailer、seccomp(sandboxd)、Landlock、密钥不进沙箱、独立审查 | AppArmor、公开的威胁模型与审计记录 |
| (文章没列但它反复提的)可见的成熟信号 | CI、单测、设计文档链 | 公开 benchmark、SLA 口径、第三方横评 |

## 要补的(按云产品的要求排,不按收益)

1. **多副本控制面**:路由与沙箱元数据进共享存储,任意副本处理任意请求;节点仍是运行态权威,控制面对账的逻辑保留。配套把节点运维(排空、均衡、版本矩阵)从调度里拆出来。
2. **节点内调度补齐**:TAP 池;按内存 / CPU / 模板快照 / 热池打分放置;优先级类(交互 > 评测 > 后台进化);带迁移的排空与在线均衡。
3. **E2B 兼容层**:在控制面前面一层翻译(它的 CubeAPI 形状),让 DSH、Codex、OpenClaw、E2B SDK 零改动接进来;沙箱里要有 E2B 的 `envd` 形状的数据面(命令 / 文件 / PTY / 端口),由 sandboxd 提供或在它旁边起一个。我们的 ABI 继续存在,是多给的一层。
4. **原地 rollback**:对运行中的沙箱打检查点、回滚,id / ticket / inbox 不变;ABI 的 `checkpoint` 从只记事件变成真的。
5. **卷**:E2B 形状,独立生命周期,跨沙箱共享;harness 目录、仓库缓存、用户数据走这里;对象存储后端。
6. **audited 出口模式**:CubeEgress 的 L7 规则是参考实现;凭证注入要做成通用的,不只对模型 API。
7. **模板带内存 + 用户提交模板**:从运行中沙箱提交成模板,rootfs 与内存快照一起。
8. **同机性能对比**:创建时延、每沙箱开销、密度。
9. 记一笔:pagemap 匿名页判增量;PVM(没有 KVM 的云主机)。

## 我们已有、它没有、要守住的

- sandboxd 的内核 ABI(预算、事件日志、report、ticket、inbox):沙箱知道它在给谁干活。它那篇 DSH 文章的边界是"Agent 是控制面,Sandbox 是执行面",每家 agent 把预算、审批、问人、交接自己做一遍;我们放在 ABI 里,换 harness 不用重做。
- 模型代理的按沙箱计量、预算、缓存计价:它不算钱。云产品要按沙箱出账单,这层我们先有了。
- ephemeral / persistent 两类沙箱与 QoS 分级:就是"批处理"与"交互"两种 SKU。
- 评测与进化:执行面之上的那一层。

## 改过的判断(第一版写错的)

- 第一版把"多副本控制面、K8s / Terraform、Redis、WebUI、ARM64、GPU"归为"它是云产品、我们是一个人的平台,不用补"。错在前提:我们也是云产品。其中 K8s / Terraform 一键部署、ARM64、GPU 透传仍可以晚做,但多副本控制面、节点调度、E2B 兼容、卷是入场券。
- 第一版说"E2B 兼容:我们的沙箱不对外卖"。错。沙箱是必争之地,争的就是外部 agent 默认选谁;E2B 形状是今天的事实标准。
- 第一版说"VM 里跑 OCI 容器我们不用"。这条保留:"VM 就是环境 + 配方层"仍是我们的选择,但**模板来源要能是 OCI 镜像**(它的 Buildkit 路径),否则用户的环境进不来。这是配方层的输入问题,不是容器层的问题。

## 两边一样的(这类系统的公理)

密钥不进沙箱、一切经代理、沙箱间永不互通、空闲即暂停、模板 = 预热到就绪的快照。它的 PVM 路线印证"没有 /dev/kvm"是普遍困境;我们在容器里用 /dev/kvm 绕过 kvm 组限制,是有 KVM 的机器上的最省解。
