# DSec 对照:他们怎么做,我们现在怎样,应该怎么做

依据 DSec 论文(arXiv 2609.22978)第 3.3、5.1 到 5.3、6.1、6.3 到 6.5、7 节,对照 `prototype/fc-node` 当前状态。首次对照 2026-10-29;第 0 节是 2026-10-30 的进展摘要,后面各节的"我们现在"列已同步更新。目标规模仍是几百到几千并发(`decisions.md` D6),所以"应该怎么做"分优先级,不是照搬。

## 0. 进展摘要(2026-10-30)

### 0.1 DSec 的能力,我们做到哪一步

| DSec 能力(论文节) | 10-29 状态 | 10-30 状态 | 差距 |
|---|---|---|---|
| edge 节点代理:准入、TTL、快照协调、网络策略(§3.3) | 单节点代理 | 同左,加 SIGTERM 排空、模板池、`disk` 规格 | 无 |
| 多实例控制面、power-of-k 放置、节点故障接管(§3.3、§7) | 无 | 单进程控制面 `controlplane.py`:注册、健康轮询、power-of-two 放置加反馈、路由持久化、转发含流式、迁移、死节点从 bundle 接管、节点回归对账;同机多容器验证(原型文档 §16) | 多实例控制面、真机多节点、BGP/ECMP 不做 |
| aether/chronus:会话、流式 IO、通道断开即失败(§3.3) | 一次性 exec | PTY 会话(open/exec/write/read/close)、流式 exec、健康看护;通道断开标记失败 | 无 |
| 三层磁盘 base/workspace/toolkit、EROFS、overlayfs(§5.1–5.3) | 整盘复制 | 只读 base + 标签层(`layers:[...]`)+ 每 VM 可写层,overlay 根;`pack` 把可写层固化为新层;开发工具层就是这么建的 | EROFS(guest 内核未编,计划随 6.18 内核一起做) |
| virtio-pmem + DAX 页缓存共享、balloon 回报、DAMON(§5.2) | 无 | pmem+DAX(内存 −71%)、balloon 空闲页回报已做 | DAMON 未做 |
| 按需镜像分发 3FS/ublk/OverlayBD(§5.3) | 无 | 分块仓库 + FUSE 按需加载 + 共享缓存 + 启动画像预取(冷启动 3.7 s 对 38 s,只拉 9%) | 内核路径 ublk 需宿主机内核 ≥ 6.0 |
| microVM 暂停/恢复、增量磁盘快照 pack_diff(§6.1、6.3) | 全量快照 | Diff 内存快照链(暂停 86 MiB 对 512 MiB)、模板快照直启 0.16 s、userfaultfd 按需供页(非 jail)、透明恢复、fork 写时复制 | 无;比论文多了内存层的 Diff 链 |
| 沙箱/模板跨节点搬运 | 无 | bundle 经分块仓库 export/import,稀疏 extent 保留;迁移 3.7 s | 对象存储换 S3 只改取块函数 |
| agent 作弊防线:CID 校验、降权、只读层、AppArmor、eBPF 白名单(§6.4、6.5) | 无 | CID 校验、agent 非特权用户、daemon 与 shell 在只读层、jailer(chroot + uid + cgroup)、每 VM tap + nftables + 出口代理白名单、输出上限 | AppArmor 未做(计划用 Landlock 替代,随 6.18 内核) |
| CPU QoS:SCHED_IDLE、core scheduling(§5.2) | 无 | 两者已做,`qos` 字段 | 无 |
| GPU/MIG、FullVM GUI(§7) | 不做 | 不做 | 不在范围 |

### 0.2 DSec 没有、我们做了的(产品层,2026-10-30)

DSec 是给 RL 训练框架用的沙箱平台,评测和打分在它之外("奖励来自退出码、测试通过率或任务专用验证器",§1),论文里没有噪声、重复、置信区间的讨论。我们的产品要在平台之上自己回答"新版本更好吗",所以多出一层:

| 组件 | 内容 | 对应文献 |
|---|---|---|
| 评测器 `eval/evaluator.py` | 任务集、版本、批次;agent 沙箱 → 导出工作区 → 独立无网络验证沙箱;基础设施失败单独计;avg@k、pass^k、半分法噪声带 δ、配对 bootstrap CI、McNemar、级联、接受门、作弊信号、导入检查 | RRSI、HGM、HarnessBank、Miller、Terminal-Bench 2.0,见 `rsi-algorithms-survey.md` |
| 模型 API 代理 `node/modelproxy.py` | 沙箱外计量:每沙箱令牌与预算、价格表、超预算 402;成本不信 agent 自报 | D4 的"成本上限放在 agent 够不到的地方" |
| 种子 agent `aios/` | mini-swe-agent 裁剪移植,Cairn stage 0,装在开发层 `/opt/aios` | DGM/SICA 的种子思路,mini-swe-agent 的 Bash-Only 榜做坐标 |
| 工作台 `web/` | 沙箱、流式执行、PTY 终端、文件、网络策略、节点、实验、批次矩阵、轨迹 | `archive/feature-design-2026-11-01.md` 第 2、3 节 |
| 开发工具层 | git、gcc、libc6-dev、make、jinja2、pytest + `/opt/aios`;apt 经出口代理走镜像 | DSec §6.1 的"agent 自建环境、构建账号分离" |

### 0.3 这一天学到的、改了的

- 评测器的任务必须来自用户自己的工作:自建玩具任务对 V4.1 Flash 全满分,噪声基线无信息,已取消(花费 0.42 美元);公开基准只做回归。下一步任务集从这个仓库本身挖(挖空带测试的函数,隐藏测试做验收),让 Cairn 改进开发这个平台的能力。
- DeepSeek V4.1 Flash 直连可用(`deepseek-flash`),第一个真实任务 7 次调用、29 秒、0.016 美元。
- CI 用带后缀的独立产品层容器,避免把正在跑真实批次的代理换成 mock。


## 0. 一个前提差异

DSec 用四种后端,其中**容器和 FnCall 跑在 QEMU 虚拟机里**,再在虚拟机里跑 Docker。我们只用 Firecracker microVM 一种后端,每个沙箱一个独立内核。这意味着:

- 论文里"agent 触发内核 bug 把节点打崩"的事故,在我们这里只会打崩那一个 microVM,这是选 Firecracker 的直接收益。
- 代价是 Firecracker 不支持 virtio-fs 和 GPU,所以分层磁盘要用块设备方案(论文 5.3 节也是这么处理 microVM 的),GUI 和 GPU 任务以后要另起 QEMU 后端。

## 1. 逐项对照

### 1.1 节点运行时:edge / aether / chronus

| | DSec | 我们现在 | 应该怎么做 |
|---|---|---|---|
| 节点组件 | **edge**(每机一个):准入检查、建存储、下发 eBPF 网络策略、拉起运行时、协调快照、TTL 回收 | 常驻节点代理 `nodeagent.py` 已做齐;其上有单进程控制面 `controlplane.py`(注册、放置、路由、迁移、接管、对账),用同机多容器验证,见原型文档 §16 | 多实例控制面、真机多节点 |
| 沙箱内代理 | **aether**:每沙箱一个,与 edge 建一条通道(容器用 UDS,VM 用 vsock);**通道断开即判沙箱失败**;按会话 ID 创建或找到 chronus,会话结束杀掉整个进程树 | `sandboxd` 兼做两层;会话有 ID 和进程组,关闭时杀进程树;节点代理健康看护,通道断开或 Firecracker 退出即标记失败 | 已完成 |
| 会话 | **chronus**:每个实例是一个独立 shell 会话;提供命令执行、文件系统、HTTP、流式 IO;一个沙箱可并发多个 | 一次性 exec、流式 exec、PTY 会话(open/exec/write/read/close)、文件读写都有;工作台终端就建在会话上 | 已完成 |
| 输出 | chronus 记录 stdout 供异步取回,曾因 `yes` 攒下几十 GB | 每流上限 1 MiB,超出截断 | 保留上限;流式模式也要有总量上限和落盘上限 |

(10-29 的 P0;10-30 已全部完成。)

### 1.2 分层磁盘与环境组合

| | DSec | 我们现在 | 应该怎么做 |
|---|---|---|---|
| 分层 | base / workspace / toolkit 三层独立版本化;升级一层只重建那一层 | 一个 1 GiB 的 ext4 整盘 | 拆成至少两层:**只读 base** + **每 VM 一块小的可写 ext4 盘**;后续再加 workspace/toolkit 层 |
| microVM 的实现 | 只读层打成 EROFS 作为**只读块设备**挂进 guest;guest 内用 **overlayfs**,下层是 EROFS,上层是 ext4 可写盘上的目录 | 无 | guest 的 `fc-init` 改为:挂载只读 base 盘和可写盘,组 overlayfs 后 `switch_root`。Firecracker 一个 VM 可挂多块 drive,只读 drive 设 `is_read_only: true` |
| 每 VM 复制 | 不复制只读层;写入只落上层 | 每个克隆 `cp` 1 GiB 整盘 | **只读 base 一个文件被所有 VM 共享,不再复制**;可写盘每 VM 新建一个空的小文件(几十 MB),创建成本接近零 |
| 只读层格式 | EROFS(压缩、随机访问、元数据与数据可分离) | ext4 | 先用 ext4 只读挂载就能跑通;之后换 EROFS,收益是压缩和 1.3 节的页缓存共享 |
| 写入位置 | 写入永远留本地盘 | 本地 | 一致,保持 |

**优先级:P0。** 这一项同时解决"克隆要复制 1 GiB"、"环境版本管理"和 1.4 的 `pack_diff`,是收益最大的一步。

### 1.3 内存与 CPU 密度

| | DSec | 我们现在 | 应该怎么做 |
|---|---|---|---|
| 页缓存共享 | 只读 EROFS 层通过 **virtio-pmem + DAX** 挂给 guest,多个 VM 共享宿主机的同一份页缓存,峰值内存降 40% | 快照克隆共享 `mem.bin`(私有映射),这是另一种共享;磁盘读入 guest 页缓存后各 VM 各一份 | 分层磁盘做完后,把只读层改用 pmem+DAX 挂载。**需核实我们用的 Firecracker v1.17 对 virtio-pmem 的支持与配置方式** |
| 空闲内存回收 | guest 内 **DAMON** 找冷页 + **virtio-balloon 空闲页回报**,时间积分内存降 21% | 无 | P2。先测量密度瓶颈再做;Firecracker 的 balloon 设备支持情况需核实 |
| 已知代价 | pmem 让峰值 CPU 从 26% 升到 41%;guest 要为 pmem 区域付 1/64 的 struct page 内存 | — | 只对小的只读层用 pmem,大的可写盘仍用 virtio-blk,和论文生产配置一致 |
| CPU QoS | 尽力而为的沙箱设 **SCHED_IDLE**;延迟敏感的开 **core scheduling**,把 SMT 干扰从 45% 降到 17% | 无 | **P1 且很便宜**:评测用的临时型沙箱的 firecracker 进程设 SCHED_IDLE(`chrt -i 0`),工作台的持久型沙箱保持普通优先级。core scheduling 需要节点代理有权限,后置 |

### 1.4 暂停、恢复、pack_diff

| | DSec | 我们现在 | 应该怎么做 |
|---|---|---|---|
| microVM 暂停 | 保存内存和执行状态为快照,**杀掉 Firecracker 进程**释放内存;恢复 = 新进程 + 加载快照 | 已实现;暂停改为 Diff 快照链(写出 86 MiB 而非 512 MiB),恢复合并链 0.28 s;模板快照直启 0.16 s(冷启动 1.06 s),预留池领取 7 ms;链恢复可走 userfaultfd 按需供页(非 jail);SIGTERM 排空把运行中沙箱暂停到盘 | 一致,见原型文档 §14–15 |
| 透明恢复 | 对暂停沙箱的**任何请求自动先恢复**再执行 | 已实现(`ensure_running`) | 一致 |
| 触发 | 训练框架主动对被抢占任务的所有沙箱发 pause | 无 | 工作台:空闲超时自动暂停(`archive/feature-design-2026-11-01.md` 2.1);实验:评测结束即销毁,不暂停 |
| pack_diff | 任意时刻对沙箱做**增量磁盘快照**,可恢复为新沙箱;用于让 agent 自己构建环境;构建账号与运行账号分离,打包前清掉可写层残留 | `POST /sandboxes/<id>/pack` 把可写层固化为新只读层;开发工具层、以后的 Cairn 版本制品都这么产出 | 一致;"打包前清残留"要在构建脚本里做(apt clean、删 lists) |
| 容器暂停 | `docker pause` + swap + `memory.reclaim` | 不适用 | 不适用 |

**优先级:透明恢复与校时 P1;pack_diff 在分层磁盘之后 P1。**

### 1.5 agent 作弊与访问控制(论文 6.4、6.5)

论文记录的真实行为,逐条对照我们的暴露面:

| 论文里的行为 | 我们现在的暴露面 | 应该怎么做 |
|---|---|---|
| 向 chronus 的 socket 伪造 RPC | `sandboxd` 监听 `VMADDR_CID_ANY`,guest 内进程可能通过本地 vsock 连上它伪造请求 | **accept 后检查对端 CID,只接受宿主机(CID 2)**;任务进程不以 root 运行 |
| 翻 chronus 日志找答案 | daemon 不在 guest 内写日志 | 保持:日志只走 vsock 出去,guest 内不留 |
| 覆盖 `/bin/bash` 注入后续会话 | `sandboxd` 用 `/bin/bash -lc`,且 agent 是 root | daemon 和它依赖的 shell 放在**只读 base 层**;任务进程降为非特权用户;daemon 用自己路径下的 shell 副本 |
| 用 `XFS_IOC_SWAPEXT` 绕过文件访问控制,顺带搞坏文件系统 | 无访问控制可绕 | 可写盘用 ext4;文件访问控制靠"任务用户无权限",而不是靠 AppArmor 事后拦 |
| 扫端口找镜像站、用 Go module proxy 拉 GitHub 代码 | 现在断网,无暴露 | 做网络时就按**白名单**来,见 1.6 |
| `grep -r /` 读 `/proc/kpagecgroup` 触发内核 bug | 只崩这一个 microVM | 节点代理检测 Firecracker 退出或通道断开,标记失败并记审计;**这是我们比论文容器后端强的地方** |
| `yes` 产生几十 GB 输出 | 已有 1 MiB 上限 | 保持,流式也要限 |
| AppArmor 限制 root 进程读写文件和 socket | 无 | 我们先用"降权 + 只读层"达到同样目的;AppArmor 作为第二层后置 |

**优先级:P0。** 只读层加降权加 CID 校验三项加起来改动很小,但把论文里大部分作弊路径堵住。

### 1.6 网络

| | DSec | 我们现在 | 应该怎么做 |
|---|---|---|---|
| 策略 | 训练框架按任务指定允许的域名或镜像服务;edge 用**每沙箱 eBPF** 按 IP、端口、协议过滤;可随任务阶段动态更新 | 没有网卡 | 每 VM 一个 tap,在节点容器的 netns 里用 **nftables 按 tap 做白名单**;规模几百到几千用 nftables 足够,不必上 eBPF |
| 域名 | 按域名或镜像服务组织 | — | 自建 DNS 转发器,解析结果同步进白名单;或直接只允许访问我们自己的出口代理,由代理按域名放行(更简单,审计也在一处) |
| 动态更新 | 支持 | — | 节点代理提供"更新白名单"接口 |
| 恢复后 | — | — | 克隆恢复时用 `network_overrides` 换 tap,重新分配 IP 和 MAC |

**优先级:P1。**

### 1.7 调度、分发与可靠性(集群级)

| | DSec | 我们现在 | 应该怎么做 |
|---|---|---|---|
| 放置 | power-of-k 采样;各实例叠加本地未上报的放置;edge 保留最终准入 | 单机 | 已写进 `archive/architecture-v0.2-2026-11-01.md`(现行见 `000`–`012`);多机时照做 |
| 镜像分发 | 3FS + EROFS 元数据本地、数据远端按需;microVM 可写盘走 OverlayBD + ublk,256 KiB 块,本地二级缓存 | 分块仓库 + FUSE 按需加载 + 共享缓存 + 启动画像预取(第 11 节):冷启动 3.7 s 对整包 38 s,只拉 9% | 论点复现;内核路径(ublk)需要更新的宿主机内核 |
| 可靠性 | BGP+ECMP 负载均衡;定期整体重建集群验证 IaC | 无 | 后置;但"控制面无持久状态、可从节点重建"这条现在就按设计执行 |
| 云突发 | 利用率超 80% 时把部分请求卸载到云 VM | 无 | 不做 |

### 1.8 GPU 与 FullVM

论文用 MIG 切分 GPU 给 FnCall 做算子评测,FullVM 走 virtio-gpu 做 GUI。我们不做;这台开发机的 GPU 被别人占着,也不在目标范围内。

## 2. 行动清单(按顺序)

**P0,已完成(2026-10-29,见 `prototype-firecracker.md` 第 7 节)**
1. **分层磁盘**:只读 base 盘(所有 VM 共享,不复制)+ 每 VM 小可写盘;guest `fc-init` 组 overlayfs。目标:克隆不再复制 1 GiB,创建成本降到建一个空文件。
2. **daemon 加固**:只接受宿主机 CID;任务进程降为非特权用户;daemon 和 shell 在只读层;输出上限保留。
3. **会话与健康**:会话 ID、进程组、结束杀进程树;节点代理保持长连接,断开即标记失败;Firecracker 进程退出即标记失败。

**P1**
4. ~~节点代理常驻化:HTTP 接口、准入、TTL、透明恢复~~ 已完成(`prototype-firecracker.md` 第 8 节)。
5. ~~恢复后校时与熵注入~~ 已随 P0 做完。
6. ~~tap 网络 + nftables 白名单 + 出口代理;恢复时换 tap 和 IP~~ 已完成(`prototype-firecracker.md` 第 9.1 节)。
7. ~~`pack_diff`:把可写盘固化为新只读层~~ 已完成(第 9.3 节);接到 RSI 版本制品的部分待做。

**P2,量上来再做**
8. ~~只读层用 virtio-pmem + DAX~~ 已完成(第 9.4 节,内存减少 71%);EROFS 未做(guest 内核未编入)。
9. ~~balloon 空闲页回报~~、~~core scheduling~~ 已完成;DAMON 未做。
10. 多节点放置;~~对象存储分发~~ 已做按需加载(第 11 节),仓库换 S3 只改取块函数。

**不做**:3FS、ublk/OverlayBD、FullVM/GPU、云突发、BGP。

## 3. 已核实的事实

- Firecracker v1.17 支持 virtio-pmem(`PUT /pmem/{id}`,可只读、可作根设备)和 balloon 的 `free_page_reporting`,以及恢复时的 `network_overrides`。均已用上。
- 论文 6.3 节容器暂停用的 `memory.reclaim` 与 `MADV_WILLNEED` 只对容器后端有意义,对我们不适用。
