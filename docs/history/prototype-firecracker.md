# Firecracker 沙箱原型:vsock 与 guest daemon

日期:2026-10-29。环境:开发机 dev-host(AMD EPYC,Ubuntu 22.04,内核 5.15),Firecracker v1.17.0,guest 内核 6.1.155(官方 CI 构建),guest rootfs 为官方 CI 的 Ubuntu 24.04。代码在 `prototype/fc-node/`。

## 1. 形态

```
宿主机 Docker 容器 fc-node (节点代理)
  --device /dev/kvm --device /dev/net/tun --device /dev/vhost-vsock --cap-add NET_ADMIN
  ├── firecracker 进程 ×N(每个 microVM 一个)
  │     ├── API: /tmp/<vm>/api.sock
  │     ├── vsock UDS: /tmp/<vm>/v.sock  (guest CID 3)
  │     └── rootfs: /tmp/<vm>/rootfs.ext4(基础镜像的可写副本)
  └── fcctl.py(宿主机侧客户端)
        └── 连接 v.sock,发送 "CONNECT 5000\n",之后是 NDJSON 流
guest
  ├── /sbin/fc-init(PID 1)
  └── /usr/local/bin/sandboxd.py(vsock 5000)
```

要点:
- 节点代理跑在容器里,**不需要宿主机的 kvm 组**,也不需要 sudo。KVM 是宿主机内核能力,容器只是透传设备,不是嵌套虚拟化。
- 基础镜像只读挂载,每个 VM 复制一份可写副本(`cp --reflink=auto`,在支持反射链接的文件系统上是写时复制)。
- guest 用自己的 PID 1,不跑 systemd,启动更快、状态更少。

## 2. 协议(sandboxd,v0)

传输:vsock,端口 5000。宿主机通过 Firecracker 的 vsock UDS 连接,先发 `CONNECT <port>\n`,收到 `OK <port>\n` 后进入 NDJSON 流;每行一个 JSON 请求,每行一个 JSON 响应,按顺序处理;同一个 VM 可以开多个连接并行。

| 请求 | 字段 | 响应 |
|---|---|---|
| `ping` | — | `uptime_s`、`pid` |
| `exec` | `cmd`(经 `bash -lc`)、`cwd`、`stdin`、`env`、`timeout`(秒,默认 60) | `exit`、`stdout`、`stderr`、`truncated`、`elapsed_s`;超时则 `exit=null, timeout=true` |
| `write_file` | `path`、`data_b64`、`mode` | `bytes` |
| `read_file` | `path`、`max_bytes` | `data_b64`、`bytes` |
| `shutdown` | — | `shutting_down`;guest 调用 `reboot(2)`,配合内核参数 `reboot=k`,Firecracker 进程正常退出 |

所有请求带 `id`,响应原样返回;错误时 `ok=false, error`。每个输出流最多 1 MiB,超出截断并标记 `truncated`。

与设计文档(`archive/architecture-v0.2-2026-11-01.md`(现行见 `000`–`012`) 第 5 节)的差距:还没有 PTY、流式输出、`seq` 事件流、`request_id` 幂等缓存、文件列表与删除。这些是下一步。

## 3. 实测结果

| 项 | 结果 |
|---|---|
| 从拉起 firecracker 到 vsock 可用 | 0.805 s(2 vCPU,512 MiB,`quiet` 启动) |
| exec 往返延迟 | 5.9 ms(`true` 命令,20 次平均) |
| exec 功能 | 退出码、stdout、超时(`sleep 5`,timeout 1 → 1.002 s 返回)均正确 |
| 文件写入与读回 | 39 字节,内容一致;写入后能被 guest 内 python3 执行 |
| 关机 | `shutdown` 后 firecracker 退出码 0 |
| 整个会话 | 2.29 s(含启动、10 余次调用、关机) |

guest 内可用:Ubuntu 24.04.3,python3.12,bash,socat,以 root 运行。

## 4. 已知问题与下一步

1. **网络**:guest 目前没有网卡,完全断网。下一步加 tap 设备、每 VM 独立子网、默认断网加白名单(nftables 在节点容器内即可做)。
3. **jailer**:尚未启用。生产前要用 jailer 做 chroot、cgroup、seccomp 和独立用户,这需要节点容器多一些权限。
4. **daemon 以 root 运行**:后续按 `archive/aios-rsi-2026-11-01.md` 的特权分离,daemon 与 Cairn 内核保留 root,可变服务和任务进程降到非特权用户。
5. **rootfs 复制**:1 GiB 的整盘复制在非反射链接文件系统上会慢;量大时改用 Firecracker 的快照加 overlay 或 OverlayBD 类方案。
6. **流式输出与 PTY**:长命令需要边跑边出,终端需要 PTY;协议要加 `exec_stream` 与 `pty_*`。
7. **启动时间**:冷启动 0.8 s 里大部分是内核与 python 启动;从快照恢复只要约 30 ms(见第 6 节)。

## 5. 复现

```bash
# 在开发机
cd <repo>
docker build -t fc-node:dev -f node/Dockerfile .
docker run --rm -v "$PWD/images:/work/images" fc-node:dev -c \
  'build-rootfs.sh /work/images/ubuntu-24.04.squashfs /work/images/sandbox-rootfs.ext4 1G'
docker run --rm --device /dev/kvm --device /dev/net/tun --device /dev/vhost-vsock --cap-add NET_ADMIN \
  -v "$PWD/images:/work/images:ro" fc-node:dev -c \
  'fcctl.py /work/images/vmlinux-6.1.155 /work/images/sandbox-rootfs.ext4'
```

机器连不上 GitHub 和 Firecracker 的 S3;二进制、内核和 squashfs 在能联网的机器上下载后 scp 到 `<repo>/{bin,images}/`。

## 6. API 控制与快照(`fcapi.py`,2026-10-29)

### 做法

- 不再用配置文件,改为启动 `firecracker --api-sock api.sock`,通过 HTTP-over-UDS 依次 `PUT /boot-source`、`/drives/rootfs`、`/machine-config`、`/vsock`,再 `PUT /actions InstanceStart`。
- **每个 VM 在自己的目录里运行,配置里只用相对路径**(`rootfs.ext4`、`v.sock`、`api.sock`)。快照会记录这些路径,所以恢复时只要换一个目录、放一份自己的 `rootfs.ext4`,同一份快照就能克隆出任意多个 VM。
- 快照:关闭 vsock 连接 → `PATCH /vm {Paused}` → `PUT /snapshot/create {Full}`,得到 `snapshot.bin`(25 KiB,设备状态)和 `mem.bin`(512 MiB,客户机内存)。然后杀掉原进程,快照加基础磁盘就是模板。
- 恢复:新进程 → `PUT /snapshot/load {snapshot_path, mem_backend: File, resume_vm: true}`,然后重新连 vsock。`mem.bin` 被所有克隆以私有映射共享,只有写到的页才复制。

### 实测(2 vCPU,512 MiB)

| 项 | 结果 |
|---|---|
| 冷启动到 vsock 可用 | 0.72 s |
| 暂停加全量快照 | 2.3 s(主要是写 512 MiB 内存文件到容器的 overlay 盘) |
| 从快照恢复到 vsock 可用 | **0.027 s** |
| 10 个克隆并行恢复 | **0.036 s** 全部就绪,单个 0.02 到 0.03 s |
| 10 个克隆的 Firecracker 进程 RSS 合计 | 368 MiB(每个配置 512 MiB,内存文件共享) |
| 状态连续性 | 快照前写入的 `/root/marker` 恢复后仍在;daemon 进程未重启,uptime 连续 |
| 克隆隔离 | 10 个克隆各自写 `/root/me` 和改主机名,互不影响 |
| 关机 | 10 个克隆退出码全部为 0 |

### 这对设计的意义

- 文档里"模板加快照恢复"的路线成立:冷启动 0.7 s 变成 30 ms,一次实验要开上千个评测沙箱时,启动不再是瓶颈。
- 磁盘是整盘复制(`cp --reflink=auto`),在不支持反射链接的文件系统上 1 GiB 复制会变慢;量大后换 Firecracker 的 overlay 或 OverlayBD 类方案。
- 快照 2.3 s 主要是写内存文件,放到 `/data` 的 NVMe 上会快;如果用差分快照(`Diff`)后续快照会更小。

### 恢复后必须处理的事

- **客户机时钟是旧的**(实测恢复后落后 2 s,放久了会差更多)。恢复后要由 daemon 校时,或者由宿主机通过 vsock 下发当前时间。
- **随机数熵**:克隆出来的 VM 内核随机数状态一样,恢复后要重新注入熵,否则各克隆生成的随机数相同。
- **网络身份**:现在还没有网卡;有了之后每个克隆要重新分配 IP 和 MAC(Firecracker 的 `network_overrides` 可以在恢复时覆盖 tap 名称)。
- **已有连接**:快照前要关闭宿主机到 guest 的 vsock 连接;恢复后重新连接。

## 7. P0 落地:分层磁盘、daemon 加固、会话与健康(2026-10-29)

对应 `dsec-vs-ours.md` 行动清单的第 1 到 3 项,外加顺手做的第 4 项里的 SCHED_IDLE 和第 5 项的校时与熵。

### 7.1 分层磁盘

- `build-rootfs.sh` 产出两个文件:`base.ext4`(只读层,417 MiB 内容)和 `upper-template.ext4`(512 MiB 表观、17 MiB 实际的空 ext4)。
- 每个 VM 的目录里 `base.ext4` 是指向共享只读层的**符号链接**,`upper.ext4` 是模板的稀疏复制。Firecracker 挂两块盘:`base` 作根设备且 `is_read_only: true`,`upper` 可写。
- guest 的 `fc-init` 两阶段:阶段 1 在只读根上把 `/dev/vda` 再挂一次到 `/mnt/lower`、把 `/dev/vdb` 挂到 `/mnt/upper`,组 `overlay(lowerdir=/mnt/lower, upperdir=/mnt/upper/upper, workdir=/mnt/upper/work)` 到 `/mnt/root`,`pivot_root` 后 `chroot` 进阶段 2;阶段 2 挂 proc/sys/dev,懒卸载旧根,常驻跑 daemon。
- 踩到的坑:CI 的 rootfs 没有 `/mnt`,而根是只读的,挂载点必须在构建 base 层时预先建好。

| 项 | 旧(单盘整盘复制) | 新(分层) |
|---|---|---|
| 创建一个 VM 的磁盘 | 复制 1 GiB | 符号链接 + 稀疏复制,13 ms |
| 克隆一个评测沙箱的磁盘 | 复制 1 GiB | 2.9 ms,实际占用 0.3 MiB |
| 冷启动到就绪 | 0.72 s | 0.78 s(多了 overlay 组装) |
| 从快照恢复到就绪 | 0.027 s | 0.062 到 0.078 s(含校时与熵注入两次调用) |
| 10 个克隆并行恢复 | 0.036 s | 0.081 s |

### 7.2 daemon 加固

- 任务默认以非特权用户 `agent`(uid 2000)运行;实测 agent 无法写 `/bin/bash`,可写自己的 home。CI 镜像自带 uid 1000 的 `ubuntu` 用户,所以 `agent` 不能用 1000。
- daemon 和它的私有 shell 副本放在只读层的 `/usr/local/lib/sandboxd/`(root 持有,0755)。
- 只接受宿主机 CID(2)的 vsock 连接。这台 guest 内核没有开 `CONFIG_VSOCKETS_LOOPBACK`,guest 内进程本来就连不上,校验是多一道保险。
- guest 内不写日志;每个输出流上限 1 MiB,流式模式同样受限。
- 平台级操作(建目录、改主机名)通过 `user: "root"` 显式指定,只有宿主机能发。

### 7.3 会话与流式输出

- `session_open`(管道或 PTY)、`session_exec`(持久 shell,cwd 与环境变量跨调用保留,`stream: true` 时按块推送 `out` 事件)、`session_write`/`session_read`(PTY 原始流,拉取式)、`session_close`(杀整个进程组)。
- 实测:`cd /tmp && export FOO=bar` 后下一条命令仍在 `/tmp` 且能读到 `FOO`;`for i in 1 2 3` 的输出在 0.0、0.3、0.61、0.91 s 逐块到达;超时(1 s)会杀掉会话并标记 `session_closed`;PTY 会话里 `tty` 返回 `/dev/pts/0`。
- 实现:管道会话用哨兵行(`__SBX_<token>_<exit>__`)切分每条命令的输出;PTY 会话在 `preexec` 里 `TIOCSCTTY` 取得控制终端,所以作业控制可用。

### 7.4 健康监测与 QoS

- 节点侧对每个 VM 保持一条专用 vsock 连接,每 0.25 s ping;连接断开或 Firecracker 进程退出即把状态置为 `failed`。实测在 guest 里触发 `sysrq c` 内核崩溃,1.09 s 内检测到,其余 9 个克隆不受影响。
- 临时型沙箱(克隆)的 Firecracker 进程设为 `SCHED_IDLE`,工作台沙箱保持普通优先级。
- 恢复后由宿主机下发当前时间(`set_time`)和 64 字节熵(`add_entropy`);实测恢复后时钟偏差 0 s,10 个克隆的 `/dev/urandom` 输出互不相同。

### 7.5 其他数字

- 10 个克隆的 Firecracker 进程 RSS 合计 470 MiB(每个配置 512 MiB,内存文件共享)。
- 快照 1.7 到 2.0 s,仍是写 512 MiB 内存文件的成本。

### 7.6 下一步(P1)

节点代理常驻化(HTTP 接口、准入、TTL、透明恢复)、tap 网络加 nftables 白名单加出口代理、`pack_diff`(把 upper 盘固化为新只读层)。

## 8. 常驻节点代理(`nodeagent.py`,2026-10-29)

对应 `dsec-vs-ours.md` P1 第 4 项,角色等同 DSec 的 edge。以容器常驻运行(`--restart unless-stopped`),VM 目录和快照放在 `/data` 的 NVMe 上(`state/`)。

### 接口

| 方法与路径 | 作用 |
|---|---|
| `GET /health` | 节点用量与预算 |
| `POST /sandboxes` | 创建:`template`、`class`(persistent/ephemeral)、`vcpu`、`mem`、`ttl_idle_s`、`ttl_max_s`;支持 `Idempotency-Key` 头 |
| `GET /sandboxes`、`GET /sandboxes/{id}`、`DELETE /sandboxes/{id}` | 列表、查询、销毁 |
| `POST /sandboxes/{id}/exec` | 一次性命令(默认 agent 用户) |
| `POST .../sessions`、`POST .../sessions/{sid}/exec|write|read`、`DELETE .../sessions/{sid}` | 持久 shell 会话(管道或 PTY) |
| `PUT/GET /sandboxes/{id}/files?path=` | 原始字节的文件上传下载 |
| `POST /sandboxes/{id}/pause`、`/resume` | 暂停(快照后杀进程,释放内存)与恢复 |
| `POST /sandboxes/{id}/fork` | 从运行中的沙箱分叉 n 个克隆(默认临时型,SCHED_IDLE) |
| `GET /audit?n=` | 节点审计日志尾部 |

### 行为

- **准入**:创建与 fork 前检查 VM 数、运行中内存合计、vCPU 合计(超卖比可配);超出返回 429 并附当前用量。这是 DSec 里"edge 保留最终准入权"的对应。
- **透明恢复**:对 paused 沙箱的任何操作先恢复再执行(DSec §6.3)。
- **TTL 循环**(每秒):空闲超时时持久型暂停、临时型销毁;最长存活到期销毁;状态 failed 的销毁。
- **fork**:父沙箱快照 → 每个克隆复制父的 upper 盘(base 仍是符号链接)→ 父与克隆各自恢复;克隆带校时与熵注入。
- **审计**:每个操作一行 JSONL,含沙箱、用户、命令前 200 字符、退出码、耗时;位于节点侧,guest 不可见。
- **并发**:每沙箱一把锁串行化操作,不同沙箱并行;每沙箱当前只用一条 vsock 连接。

### 实测(端到端 HTTP 测试 `test-node.py`,全部通过)

| 项 | 结果 |
|---|---|
| 创建到就绪 | 0.83 s |
| 暂停(快照到 NVMe 并杀进程) | 0.56 s(此前写到容器 overlay 盘要 2 s) |
| 对暂停沙箱 exec,自动恢复并返回 | 0.07 s |
| fork 3 个克隆(含父恢复) | 0.86 s |
| 克隆继承父文件、相互独立、父不受克隆写入影响 | 通过 |
| 内存超预算 | 429 拒绝 |
| 空闲 TTL | 临时型 3 s 后销毁,持久型 4 s 后自动暂停 |
| 幂等创建 | 同一 `Idempotency-Key` 返回同一沙箱 |

### 踩到的坑

- 在同一目录重启 Firecracker 时,上一个进程留下的 `api.sock`、`v.sock` 会让新进程绑定失败。启动前必须清掉。之前的克隆演示都在新目录里恢复,所以没暴露。
- 容器以 root 写的状态文件宿主机普通用户删不掉,清理要通过容器做;后续节点代理应以非 root 用户运行,或用用户命名空间映射。
- 幂等缓存要在沙箱销毁时同步清掉,否则同一个 `Idempotency-Key` 的重放会返回一个已经不存在的沙箱 ID。第二轮测试暴露了这个问题,已修。

### 还没做

流式 exec(HTTP 分块或 WebSocket)、重启后的状态重建(当前重启会丢失注册表,磁盘上的 VM 目录变成孤儿)、jailer、网络、`pack_diff`。

## 9. 单机机制补齐:网络、恢复、流式、分层与 pack、内存与 QoS(2026-10-29)

对应 `dsec-vs-ours.md` 的 P1 第 6、7 项和 P2 第 8、9 项的大部分。三套测试全部通过(`test-node.py` 15 项、`test-net.py` 15 项、`test-layers.py` 8 项)。

### 9.1 网络

- 每个 VM 一个 tap,各自一个 /30(池 10.200.0.0/16,1.6 万个);tap 之间互不相通,所以克隆带着相同 MAC 恢复也无妨。tap 名用沙箱 ID 的哈希,避免 15 字符截断后同名(第一次 fork 就撞上了这个坑)。
- nftables:forward 默认 drop;input 只放行 `tap-*` 到本节点 3128 端口;按沙箱可加直连 CIDR(单独的 chain,带 masquerade)。
- 出口代理(`egress_proxy.py`)跑在节点代理进程里,CONNECT 和明文转发;按 guest IP 找到沙箱的 `allow_hosts`(支持通配),不在名单里的返回 403;每次决定写审计。域名解析在代理侧完成,guest 不需要 DNS。
- guest 侧 `net_config`:宿主机在启动、恢复、fork 后推送 IP、网关、代理地址;daemon 把代理写进任务进程的环境变量、`/etc/profile.d` 和 apt 配置。
- 恢复或 fork 时用 `network_overrides` 换 tap,再推送新 IP。
- `PUT /sandboxes/{id}/network` 动态改白名单。

实测:默认拒绝 403;加白名单后 https(CONNECT)和明文都 200;非白名单域名被拒;绕过代理直连超时;通过代理真实下载 11.5 KB 0.4 s;fork 后父和两个克隆 IP 不同且都能出网;暂停后透明恢复网络仍在。

### 9.2 节点代理补齐

- 重启恢复:每个沙箱目录有 `spec.json`;启动时扫描,有 `paused/snapshot.bin` 的注册为 paused(重新分配 tap,恢复时换 tap),其余视为孤儿删除。实测 `docker restart` 后暂停的沙箱回来,exec 自动恢复且网络正常。
- 流式 exec:`POST /sandboxes/{id}/exec/stream`,分块 NDJSON,事件逐块到达(0.01、0.31、0.61、0.91 s)。
- vsock 客户端和 daemon 的读取改为带缓冲:无缓冲的 `readline()` 是逐字节 `recv`,传 20 MB 要 31 s,改后 0.44 s。

### 9.3 分层与 pack_diff

- 磁盘按文件系统**标签**识别(`sbx-base`、`sbx-upper`、`sbx-layer-*`),不依赖设备顺序。额外层以只读 drive 挂入,fc-init 把它们叠在 base 之上(后加的层优先级高)。
- `POST /sandboxes/{id}/pack`:guest 把 overlay 的 upper 目录打成 tar(保留 xattr 与 whiteout 字符设备)→ 宿主机分块读回 → 解包 → `mkfs.ext4 -d` 成只读层并注册;`POST /sandboxes` 的 `layers: [id]` 使用它。
- 实测:20 MB 的改动打包 0.59 s(tar 0.05 s,传输 0.44 s),层镜像 48 MB;新沙箱能看到层内容;**层里的删除(whiteout)在新沙箱中生效**;层不可变,一个沙箱的写入不影响另一个。
- 局限:`trusted.overlay.opaque` 这类 xattr 在容器里解包时可能设不上,"整目录替换"会退化成"合并";镜像大小按内容两倍估算,没有精确计算。

### 9.4 内存与 QoS

| 机制 | 做法 | 实测 |
|---|---|---|
| **virtio-pmem + DAX** 共享只读层 | base 以 `/pmem/base`(只读)作根设备,内核参数 `rootflags=dax`,fc-init 把 lower 以 `ro,dax` 挂载 | 6 个 VM 各读一遍 base 文件:virtio-blk 下节点 cgroup 内存增加 **2141 MiB**,pmem+DAX 下 **610 MiB**,减少 71%;读取也更快(8.6 s 对 5.5 s) |
| **balloon 空闲页回报** | `/balloon` 预置 `free_page_reporting: true` | 分配并释放 600 MiB 后,关闭时 6 s 后仍占 604 MiB,开启时降到 27 MiB |
| **core scheduling** | `qos: ls` 的 VM 用 `prctl(PR_SCHED_CORE_CREATE)` 建 cookie | 创建成功;未做干扰对比测试 |
| SCHED_IDLE | 临时型沙箱 | 已有 |

没做的:DAMON(需要 guest 内核参数和 sysfs 调优,先等有密度瓶颈再说);EROFS(guest 内核没编);jailer(需要容器有 SYS_ADMIN 和可写 cgroup)。

### 9.5 文件

`prototype/fc-node/node/`:`netmgr.py`、`egress_proxy.py`、`nodeagent.py`、`fcapi.py`、`sbxclient.py`、`guest/fc-init`、`guest/sandboxd.py`、`build-rootfs.sh`、三个测试脚本;`run-node.sh` 为启动命令。

## 10. jailer 加固(2026-10-29)

每个 VM 由 Firecracker 的 **jailer** 启动:chroot 到自己的目录、以独立的非 root uid 运行、无任何 capability、per-VM cgroup 限额(内存 = guest 内存 + 256 MiB、CPU = vCPU 数、pids 256)。四套测试(38 + 12 项)在 jail 模式下全部通过。

### 做法

- jail 根目录 `state/jail/firecracker/<id>/root` 就是 VM 目录;镜像目录和层目录以**只读 bind mount** 挂进去(`/images`、`/layers`),所有 API 路径改为 chroot 内的路径;API socket 在 `/run/firecracker.socket`。
- uid:每个沙箱一个(10000 起),克隆用父 uid 加偏移;gid 统一 10000,快照文件 0640 让克隆能读父的内存文件(硬链接进克隆的 chroot)。
- tap 创建时指定 `user/group` 为该 uid,Firecracker 不需要 NET_ADMIN 就能打开。
- cgroup:节点代理启动时把容器的 cgroup 树重挂为可写、把自己挪到 `init` 叶子、在根上开启 `+cpu +memory +pids`,jailer 再在 `fc/<id>` 下建组;销毁时删组。
- 节点容器需要:`--cap-add SYS_ADMIN`(bind mount、pivot_root)、`--cap-add SYS_PTRACE`(给别的 uid 的进程设 core-scheduling cookie)、`--cgroupns=private`、`--security-opt apparmor=unconfined --security-opt seccomp=unconfined`(docker 默认策略拦 mount 和 pivot_root)。这些只给受信任的节点容器,沙箱里的代码在 microVM 内,碰不到。

### 实测(`test-jail.py`)

| 项 | 结果 |
|---|---|
| VMM 进程 uid | 10008(非 root),`CapEff` 全零 |
| chroot | VMM 的 `/` 是 jail 目录(由 `mountinfo` 验证) |
| cgroup | `fc/<id>`:memory.max 805306368、cpu.max `200000 100000`、pids.max 256 |
| 网络 | jail 内的 VM 通过属于自己 uid 的 tap 正常出网 |
| fork | 克隆各自独立 uid,从父的快照硬链接恢复 |
| 暂停与透明恢复 | 正常 |
| 超额分配 | 256 MiB 的 guest 分配 700 MiB:guest 内 OOM,节点不受影响 |
| 清理 | 销毁后 jail 目录、bind mount、cgroup 全部清掉 |

### 踩到的坑(都是真 bug,已修)

1. **跨沙箱误删**:清理时用 `dirname(dirname(<id>/root))` 算目标,得到的是整个 `jail/firecracker`,销毁任何一个沙箱会清空所有沙箱的文件,只剩挂载点。这导致了 TTL 自动暂停失败(API socket 没了)和重启恢复失败(`spec.json` 没了),花了几轮才定位。现在只取一级 `dirname`,并断言目标目录名等于沙箱 ID。
2. `os.path.ismount` 对同一文件系统内的 bind mount 返回 False,导致 `layers` 挂载没卸、目录删不掉;改为 `mountpoint -q` 加 `umount -l` 循环。
3. docker 默认 seccomp 不放行 `pivot_root`(即使有 SYS_ADMIN),jailer 报 EPERM;需要 `seccomp=unconfined`。
4. 容器没有 `SYS_PTRACE` 时,读别的 uid 进程的 `/proc/<pid>/root` 和对其做 `prctl(PR_SCHED_CORE)` 都会 EPERM。
5. docker build 的上下文会扫到 root 持有的 `state/` 目录,需要 `.dockerignore`。

### 没做

AppArmor profile(论文用它限制沙箱内 root 进程读写文件和 socket;我们靠非 root 用户加只读层达到同样目的,AppArmor 作为第二层后置);节点代理自身仍以 root 运行(jailer 需要)。

## 11. 镜像分发与按需加载(2026-10-29)

对应 DSec §4.4、§5.3(3FS 上的 EROFS 元数据本地、OverlayBD + ublk 按需块加载)。这台机器没有 ublk(内核 5.15)和 nbd 模块,也搭不了 3FS,所以用等价的组合复现同一个论点:**启动只需要镜像的一小部分,按需加载比整包拉取快一个数量级,而且本地缓存可以被所有 VM 共享**。

### 做法

| 论文 | 我们 |
|---|---|
| 3FS 分布式文件系统 | `imgstore.py`:内容寻址的分块仓库(1 MiB 块,sha256 去重,零块只存一份),HTTP 服务,可限速模拟远端;接口留给 S3 |
| OverlayBD + ublk 按需块加载,256 KiB 块,本地二级缓存 | `lazyfs.py`:FUSE 文件系统,读到哪块取哪块,本地内容寻址缓存(NVMe)被所有镜像和 VM 共享,预读 4 块 |
| EROFS 元数据留本地 | **启动画像预取**:记录首次启动碰到的块,之后的节点先拉这些块 |
| 写入留本地 | 不变:可写层仍是每 VM 的本地 ext4 盘 |

节点代理以 `LAZY_STORE` 启动时挂载 lazyfs,模板的内核和 base 层指向懒加载路径;jail 模式下把这个 FUSE 挂载 bind 进每个 chroot(`allow_other`)。

### 实测(仓库限速 200 Mbps,base 1 GiB,内核 43 MiB)

| 场景 | 就绪时间 | 拉取量 |
|---|---|---|
| 整包下载再启动(基线) | 37.9 s + 0.8 s | 1024 MiB |
| **冷缓存按需启动** | **3.7 s** | 189 MiB(含预读;实际碰到 92 块 = **9%**,论文报告 4%~13%) |
| 热缓存启动 | 1.1 s | 0 |
| 清空缓存、先预取启动画像再启动 | 0.9 s 预取 + 1.5 s 启动 | 画像 92 MiB + 启动期 30 MiB |
| 6 个 VM 冷缓存并发启动 | 3.6 s 全部就绪 | 188 MiB 合计(缓存共享,不是 6 倍) |
| pmem+DAX 的 base 叠在懒加载文件上 | 0.9 s | 0(热) |

结论和论文一致:按需加载比整包拉取快约 10 倍;且随着并发上升,远端流量不随 VM 数线性增长。

### 局限

- FUSE 走用户态 Python,单次读 1 MiB,冷路径的吞吐和延迟不如内核 ublk/EROFS;热路径走页缓存,影响小。要上内核路径需要宿主机内核 6.0 以上(ublk)或可加载 nbd 模块。
- 画像是"碰过的块索引",不是 EROFS 那种元数据与数据的分离;对 ext4 这是最接近的替代。
- 仓库是 HTTP 单机服务,没有鉴权和多副本;换成 S3 只改 `lazyfs` 的取块函数。
- 踩的坑:FUSE 动态生成的控制文件大小在 `getattr` 与 `read` 之间变化会让读者拿到截断的 JSON,控制文件要定长填充。

## 12. 版本控制与 CI(2026-10-29)

开发机连不上 GitHub,在它上面用 Docker 自建了 Gitea 1.22(容器 `fc-gitea`,sqlite,只绑定 127.0.0.1:13000,通过 SSH 隧道访问),开启 Gitea Actions;`fc-runner`(act_runner 0.6.1)挂 docker socket,任务容器接入 `fcnet`,因此能直接访问正在运行的节点代理 `fc-node:8800` 和分块仓库。

- 仓库 `William2333ZZ/cairn`,工作区就是 `<repo>`;二进制、镜像、状态、仓库数据不入库。
- `actions/checkout` 需要访问 GitHub 或 gitea.com,机器出不去,所以工作流里用 `git clone http://fc-gitea:3000/...` 自行检出。CI 镜像 `fc-ci:dev` 是节点镜像加 git。
- 首次推送触发的运行:`lint` 1 秒通过;`integration` 76 秒通过(`test-node` 15 项 + `test-layers` 8 项,全部 OK)。
- 需要 docker 权限的套件(网络与重启恢复、jail、懒加载)没有进 CI,仍手动跑;要进 CI 得给 runner 的任务容器挂 docker socket,或改用 host 模式的 runner。
- 服务刻意不暴露到公网:该机器是公网 IP、密码登录,先用隧道。

## 13. 加固与工程化(2026-10-29,CI 全套接入之后)

约束:开发机是多人共用的,不做压测;这一轮全是正确性、安全和工程质量。

| 项 | 做法 |
|---|---|
| 节点 API 鉴权 | 所有路由(除 `/health`)要求 `Authorization: Bearer <token>`;token 由 `NODE_TOKEN` 指定,或首次启动生成到 `state/node-token`(0600)。无 token 返回 401。测试脚本从环境变量 `NODE_TOKEN` 取;CI 从挂载的状态目录读取后写入 `$GITHUB_ENV` |
| 锁拆分 | 沙箱锁只保护生命周期操作(创建、暂停、恢复、fork、销毁、透明恢复)。exec、流式 exec、会话操作各自新开一条 vsock 连接,不再持锁;一条长命令不会阻塞同沙箱的其他请求 |
| 快照一致性 | 快照前 guest 执行 `sync` 并 `fsfreeze --freeze /run/upper`(可写层),再暂停和落盘;恢复后 `fsfreeze --unfreeze`(幂等)。快照里的可写层不再是"写到一半"的状态 |
| 懒加载缓存上限 | `lazyfs` 后台每 10 秒检查缓存总量,超过 `LAZY_CACHE_MAX_MIB`(默认 4096)时按访问时间淘汰到 90% |
| 出口代理限流 | 每沙箱同时最多 32 条出站连接,超出返回 429 并记审计 |
| 单元测试 | `tests/unit/`,pytest,13 项:出口策略匹配(通配、大小写、空列表、`*`)、tap 的 MAC 与地址池、沙箱规格校验与准入预算、token 生成与权限、lazyfs 的分块读取与去重(用进程内的分块仓库,不需要 FUSE 挂载)。CI 的 `lint` 任务里运行 |
| CI 全套接入 | `suite` 任务顺序跑五套端到端测试;任务容器挂宿主机 docker socket(act_runner 自带)和节点工作目录(`valid_volumes`),所以能重启、重建、`docker exec` 节点容器 |
| 更轻的 CI | 密度对比 6 个 VM 改为 3 个;balloon 测试 1 GiB VM 改为 512 MiB;懒加载并发 6 改为 3;懒加载测试先清缓存再测"冷启动",断言改成确定性的"预取后启动期拉取更少" |

踩到的坑:
- act_runner 已经把 docker socket 挂进任务容器,再在 `container.options` 里挂一次会报"重复挂载点";额外卷必须列进 `valid_volumes`。
- 单元测试里用重复模式生成的 3 MiB 数据,三个块哈希相同被去重,只拉了 2 个对象。测试期望写错了,但顺带验证了去重。
- Mac 侧的 `.gitignore` 覆盖了开发机的,少了 `state/`,`git add` 去索引 root 持有的 token 文件而失败;两边已统一。

CI run 4(本轮改动推送后):`lint` 通过(编译、shell 语法、13 项单元测试),`suite` 五套端到端 57 项全部通过,未鉴权请求返回 401。

没做(有意):性能压测、DAMON、EROFS、AppArmor、运行中沙箱在代理重启后的重接(容器重启会连带杀掉所有 VM,重接没有意义;只有进程级重启才需要)。

## 14. 巩固节点:模板快照直启、Diff 快照链、排空重启(2026-10-30)

仍然不做压测;这一轮改的是启动路径、暂停的成本和节点重启时的行为。所有数字来自单个 VM 的端到端测试,不是压力数据。

### 14.1 Diff 快照与内存链(`node/memchain.py`)

Firecracker 开启 `track_dirty_pages` 后,`Diff` 快照只写自启动或上次加载以来被写过的页,文件是稀疏的。我们把一个 VM 的内存表示为一条**链**:第一个文件可直接恢复(自启动的 Diff,未写过的页读出来是零;或一个完整文件),后面是零到多个 Diff 层。

| 操作 | 做法 |
|---|---|
| 快照 | 默认 Diff;`chain.json` 记录它之下的层。文件 0640,同 gid 的其他 VM 可读 |
| 恢复 | 链长 1:直接映射(多个沙箱从同一文件恢复,共享宿主机页缓存)。链长大于 1:稀疏复制第一层,再把后续层的数据区段(`SEEK_DATA/SEEK_HOLE`)写上去,得到一个合并文件;加载后立即 `unlink`,映射让 inode 活着,磁盘上只留各层。`/snapshot/load` 带 `track_dirty_pages: true`,所以下一次暂停又是 Diff |
| 暂停 | Diff 到 `mem/<时间戳>.bin`;自有层超过 3 个时折叠成一个稀疏文件,删掉被替代的层 |
| fork | Diff 后立刻折叠成一个稀疏文件,所有克隆硬链接并映射同一个文件 |
| 文件位置 | 链里不在本 VM 目录(jail 的 chroot)下的文件,硬链接进 `ext/`;删掉模板或父沙箱不影响已经启动的沙箱 |

测得:512 MiB guest 暂停写出的层 86 MiB(原来整份 512 MiB),暂停 0.25 s;恢复时合并两层 0.19 s,整个恢复 0.28 s(原来 27 ms,代价是每次恢复多一次稀疏复制);4 次暂停恢复后链被折叠为 143 MiB 一个文件,状态完整。fork 后父沙箱的内存文件 78 MiB(原来 Full 快照 512 MiB)。

### 14.2 模板快照直启(`/templates/snapshot`)

`POST /templates/snapshot` 用一份规格(模板、vCPU、内存、pmem/balloon、层)启动一次 guest,等 sandboxd 就绪并静置 0.5 s,做自启动的 Diff 快照,连同启动后的可写盘保存到 `state/templates/<key>/`。key 是 guest 配置加镜像的(大小、mtime)的哈希,所以镜像换了、设备集变了都不会错用旧快照;lazyfs 给镜像文件报告内容派生的 mtime,保证重挂载后 key 不变。

创建沙箱时 `boot` 取 `auto`(有匹配的模板快照就用)、`cold`、`snapshot`(没有就 400)。从模板恢复后做的事:解冻可写层、校时、注入熵、推送网络配置、改主机名。

测得:从模板快照创建 0.16 s,冷启动 1.06 s;模板内存文件 74.7 MiB(Diff 自启动),可写盘 0.5 MiB。这正是 DSec 里"预热的快照池"的单机版本:同一模板的所有沙箱映射同一个内存文件,页缓存共享。

### 14.3 SIGTERM 排空

节点代理收到 SIGTERM 时:拒绝新建(503),并发地把运行中的持久沙箱 Diff 暂停到盘、销毁临时沙箱和失败沙箱,写完审计后退出;`run-node.sh` 给容器 `--stop-timeout 120`。重启后 `recover()` 把它们按暂停态接回,第一次操作透明恢复。测得:1 个运行中沙箱加 1 个临时沙箱,`docker restart` 共 4.1 s,恢复后内存里写的标记和盘上的文件都在。

此前"容器重启会丢掉运行中的沙箱"是真的;现在运行中的持久沙箱在计划内重启时不丢。非计划(SIGKILL、宿主机掉电)仍然丢运行态,只剩上一次暂停的快照。

### 14.4 其他

- 幂等键缓存有上限(2000),满了淘汰最旧的十分之一。
- `fork` 的克隆改自己的主机名。
- 单元测试新增 6 项(内存链的区段识别、按序叠加、单文件复制;`boot` 校验;模板 key 只取决于 guest 配置;排空时拒绝创建),共 19 项。端到端新增 `test-snap.py` 26 项,进 CI,包含 `docker restart` 的排空恢复。

踩到的坑:
- lazyfs 原来给文件报告"当前时间"作为 mtime,模板 key 每秒都变;改为按分块哈希派生。
- 恢复后需要从 `chain.json` 重新读出链,否则暂停态的 `mem_chain` 显示的是恢复前的链。
- fork 用 Full 快照写满 512 MiB;改成 Diff 后折叠,只写有数据的页。

没做:用 Firecracker 的 UFFD 后端做"不复制的链合并"(可以把恢复的 0.19 s 合并去掉,但要写一个页错误处理进程);模板快照的自动预热和按模板预留池;多节点间共享模板快照(文件放到分块仓库即可,接口已经是文件)。

## 15. 无复制的链合并(userfaultfd)、模板预热与预留池(2026-10-30)

### 15.1 userfaultfd 处理器(`node/uffd_handler.py`)

§14 里链长大于 1 的恢复要先合并出一个文件(0.19 s)。Firecracker 的 `Uffd` 内存后端把这一步去掉了:VMM 匿名映射 guest 内存,把 userfaultfd 和各内存区的布局(JSON 数组,`base_host_virt_addr`、`size`、`offset`、`page_size`,描述符走 SCM_RIGHTS)通过一个 unix socket 交给我们,之后 socket 上不再有通信。处理器收到缺页事件后,从链里最上层有数据的那一层 `UFFDIO_COPY`,没有任何层有数据的页用 `UFFDIO_ZEROPAGE`。

| 点 | 做法 |
|---|---|
| 页归属 | `PageMap`:对每一层做 `SEEK_DATA/SEEK_HOLE`,自下而上填一个"页 → 层"的字节数组,上层覆盖下层 |
| 按块服务 | 一次缺页把所在 2 MiB 块里所有尚未填充的页一起填上(同源连续页合成一次 ioctl),所以 94 MiB 的内存只用了 47 次缺页往返 |
| balloon | guest 归还的页触发 `UFFD_EVENT_REMOVE`;处理器记下这些页,之后再缺页就给零页,不再从文件读旧内容。`EAGAIN`(remove 与缺页竞争)时先排空事件再重试 |
| 位置 | socket 在 VM 目录(jail 的 chroot 根)下,属主是 VMM 的 uid,0600;处理器是节点代理里的一个线程,VM 被杀时随之停止,统计留在沙箱信息里 |

测得(非 jail,512 MiB guest,链长 2):恢复到可执行命令 0.21 s,47 次缺页,复制 23106 页、零页 958 页,处理器累计 0.07 s,无合并文件;第二次暂停后链长 3,再次经 uffd 恢复正常;从 uffd 支撑的 VM 做 Diff 快照和 fork 都正常;balloon 归还 448 MiB 页触发 113 次 REMOVE,全部被跟踪。

**限制**:创建 userfaultfd 需要 `CAP_SYS_PTRACE` 或宿主机 `vm.unprivileged_userfaultfd=1`。开发机是 0 且共用,不改。jailer 会丢掉全部 capability,所以 **jail 模式下用不了 uffd**,节点启动时探测并回退到 §14 的合并文件,`/health` 里 `uffd` 和 `uffd_note` 说明原因。CI 里先跑 jail 模式(验证回退),再以 `JAIL=0` 重启节点跑 uffd 段,最后恢复 jail。要在 jail 下用 uffd,需要自己写启动器(chroot + 新 pid 命名空间 + 以 ambient capability 保留 `CAP_SYS_PTRACE`),或在专用宿主机上打开那个 sysctl。

### 15.2 模板预热与预留池

`PUT /templates/pool` 带规格和 `ready: N`:节点确保模板快照存在(没有就构建),后台维持 N 个从它恢复出来的、已联网的热沙箱(不在 `/sandboxes` 列表里,但计入 `/health` 的用量)。`POST /sandboxes` 的规格 key 匹配时直接领走一个:套上调用方的网络策略和 qos(运行中改调度类,需要 `SYS_NICE`)、校时、注入熵、登记、改 TTL 和类别;池子在后台补齐。`TEMPLATE_POOL` 环境变量可以在节点启动时预热,例如 `[{"mem":512,"vcpu":2,"ready":2}]`。排空时池里的 VM 一并销毁;池中 VM 挂了会被剔除。

测得:领取一个热沙箱 **7 ms**(模板直启 0.16 s,冷启动 1.04 s),领走后 1 秒内补齐。

| 创建方式 | 时间 |
|---|---|
| 冷启动 | 1.04 s |
| 模板快照直启 | 0.16 s |
| 预留池领取 | 0.007 s |

### 15.3 其他

- `run-node.sh` 重建容器前先 `docker stop`(SIGTERM),所以手工重启也走排空,运行中的持久沙箱不丢。
- `VM.apply_qos` 对 VMM 的全部线程改调度类;原先只在进程刚启动时设置一次。
- 单元测试 21 项(新增 `PageMap`:按最上层分组、跳过已填充页、归还页变零页);`test-snap.py` 增加池和 uffd 两段,共 39 项。

踩到的坑:
- `docker rm -f` 是 SIGKILL,排空根本没跑;脚本里必须先 `docker stop`。
- `mirrors.aliyun.com/` 根路径本身返回 403,测试里要用 `/ubuntu/` 判断代理放行。
- 加载失败(uffd 建不出来)时要把处理器线程停掉、进程杀掉,沙箱保持暂停态可重试。

没做:jail 下的 uffd(见上);多节点共享模板快照;按预测自动调池子大小。

## 16. 控制面与多节点(同机多容器模拟,2026-10-30)

多个 `fc-node` 容器各有自己的网络命名空间、状态目录、端口和 token,对控制面来说就是独立节点。**控制面语义**(放置、路由、迁移、接管、对账)在这种模拟里和真机一致;**物理层**(跨机页缓存失效、真实网络带宽、全局内存压力、真正的宕机)测不出来,下面的数字都是同机模拟,只用来验证机制,不代表性能。

### 16.1 节点间搬运:bundle

暂停态沙箱 = 内存链各层 + 设备状态 + 可写盘 + 规格;模板快照 = 内存 + 状态 + 可写盘。两者都能作为 **bundle** 发布到分块仓库(`imgstore` 加了 HEAD/PUT),另一节点拉回。分块按内容去重,所以模板那一层在仓库里只有一份,迁移一个沙箱实际只传它自己的 Diff 层(测得 sent 0 MiB,因为模板层和它暂停时自动发布的层都已在仓库里)。

踩的坑(严重):Diff 层的洞表示"这页没改",按 1 MiB 分块传输后洞被填成零,合并时把模板内容覆盖成零,guest 恢复后立刻三重错误(`Unexpected exit reason on vcpu run: Shutdown`)。修法:推送时把文件的数据区段表写进清单,拉取时只写区段内的字节。这条对任何用对象存储搬运稀疏内存快照的实现都成立。

`durable` 沙箱(控制面给持久沙箱默认打开)每次暂停后自动发布 bundle;这就是节点挂掉后还能在别处恢复的前提。

### 16.2 控制面(`node/controlplane.py`)

| 职责 | 做法 |
|---|---|
| 注册与健康 | 节点列表来自环境变量或 `POST /nodes`,每 2 秒轮询 `/health`,记录存活和用量 |
| 放置 | power-of-two:从有余量的节点里抽两个,取运行 VM 少的;有热池或已有该模板快照的节点优先。创建/删除后立刻在本地用量上做反馈,节点若回 429 则标满、换一个重试 |
| 路由 | 沙箱 ID → 节点,落盘;`/sandboxes/<id>/...` 全部转发到所属节点,流式响应按块透传 |
| 迁移 | 源节点暂停 + 发布,目标节点导入,源节点删除,改路由 |
| 接管 | 节点不可达时其沙箱显示 `unreachable`、操作返回 503;`POST /nodes/<n>/evacuate` 把它的 durable 沙箱从最近一个 bundle 导入到别的节点 |
| 对账 | 节点回来后,它持有但路由指向别处的沙箱被删掉(旧副本),路由指向它但它没有的被清掉,它有而我们不知道的被收编 |
| 模板 | `POST /templates/everywhere`:在一个节点构建、发布,其余节点导入 |

### 16.3 对比:单节点 vs 两节点(每节点上限 2 个 VM、1 GiB)

| 项 | 单节点 | 两节点经控制面 |
|---|---|---|
| 可容纳的 256 MiB 沙箱 | 2,第 3 个 429 | 4,分布 2/2,第 5 个 429 |
| exec 往返中位数 | 6.4 ms(直连节点) | 8.5 ms(多一跳约 2 ms) |
| 模板快照 | 本地构建 | 一处构建,其余导入,共 3.3 s |
| 迁移暂停态沙箱 | 不适用 | 发布 3.0 s(主要是哈希与逐块 HEAD),导入 0.4 s(拉 132 MiB,本地仓库),总 3.7 s |
| 节点挂掉 | 沙箱不可用 | 2 到 4 秒内标记不可达;`evacuate` 后在另一节点恢复,内容为最后一次暂停时的状态;节点回来后旧副本被对账删掉 |
| 流式 exec | 直连 | 透传,分块到达时序不变 |

端到端 `test-cp.py` 19 项全部通过,进 CI(CI 自己起 `fc-node-2`、`fc-node-3` 和 `fc-cp`,跑完删掉)。

### 16.4 CI 自身的一个漏洞

run 7 显示全绿,但 `test-cp` 根本没跑:那一步先 `cd` 到节点目录再 `cd src`,找不到目录,失败被 `&&` 链吞掉,最后的"日志里没有 FAIL"检查读到的是上一步留下的日志。改法:用绝对路径;每步先删日志;每个端到端脚本退出时打印 `RESULT ok=N fail=M`(崩溃也打印),CI 同时要求脚本退出码为 0、日志里有 `RESULT`、没有 FAIL。教训:"没有 FAIL"不等于"通过",必须有正向证据。

仓库也加了清理:`DELETE /bundles/<name>`、`POST /gc`(删掉 60 秒以上无人引用的块);节点销毁 durable 沙箱时删掉它的 bundle。几轮测试攒下的 1.6 GB 清到 431 MB。

### 16.5 还差的

- 发布 3 s 偏慢:逐块 HEAD 可以换成一次批量查询;内容哈希可以在暂停时顺带算。
- 接管依赖"最后一次暂停",运行中的非 durable 状态在节点挂掉时丢失,和 DSec 一样。
- 控制面是单进程、路由表是本地文件;多实例需要共享存储,D6 里的接缝已留。
- 物理层数字(真网络、跨机缓存)要专用机器。

## 17. 产品层第一轮:评测器、模型代理、种子 agent、工作台(2026-10-30)

依据 `rsi-algorithms-survey.md` 第 6 节的设计结论实现;接口约定在 `prototype/fc-node/CONTRACT.md`。

### 17.1 组成

| 组件 | 位置 | 作用 |
|---|---|---|
| 种子 agent(Cairn stage 0) | `aios/`(装进开发层的 `/opt/aios`) | mini-swe-agent 2.4.6 的裁剪移植:一个 bash 工具、一个循环、提示词原样保留;去掉 litellm/pydantic/rich,模型客户端用标准库,依赖只剩 jinja2;配置是 JSON;成本从代理响应头取,不自报。约 330 行 |
| 模型 API 代理 | `node/modelproxy.py`,容器 fc-model(:18500) | OpenAI 兼容;每个沙箱一个令牌、预算,按价格表计量,超预算 402;后端 `upstream`(DeepSeek 等 OpenAI 兼容端点)或 `mock`(确定性脚本,CI 用) |
| 评测器 | `eval/evaluator.py`,容器 fc-eval(:18600) | 任务集、版本、批次(baseline/compare/check)、运行;SQLite;每次运行 = agent 沙箱 → 导出工作区 → 销毁 → 独立无网络验证沙箱跑验收;基础设施失败重试 3 次并单独计数;统计:avg@k、pass@1、pass^k、半分法噪声带 δ、配对差 + bootstrap CI + McNemar、接受判定;级联(烟测 → 全量);作弊信号;导入检查(参考解过、空解不过、重复一致、时长) |
| 任务集 | `eval/tasksets/basic/` | 10 个小任务(编码 4、运维 3、数据 2、文本 1),7 演化 / 2 验证 / 1 测试,3 个烟测,2 个带变体输入 |
| 工作台 | `web/`(控制面 `/ui/` 直接提供) | 沙箱列表与创建、详情(流式执行、PTY 终端、文件、网络策略、暂停/恢复/fork/打包)、节点、实验(任务集、版本、批次)、批次矩阵、运行轨迹查看器;原生 JS,无外部资源 |
| 开发工具层 | `node/build-dev-layer.sh` → `state/dev-layer-id` | git、gcc、libc6-dev、make、jq、python3-jinja2、python3-pytest 和 `/opt/aios`;apt 经出口代理走 aliyun;681 MiB |
| 控制面扩展 | `node/controlplane.py` | 静态 `/ui/`、`/eval/*` 转发、路由保存加锁;`./run-node.sh cp 0` 把主节点纳入 |
| 沙箱规格 | `disk`(512..16384 MiB) | 可写层离线扩容(truncate + resize2fs),进模板键 |

### 17.2 跑通的数字(单节点,mock 模型,全部 512 MiB VM)

| 项 | 数值 |
|---|---|
| 导入检查 10 任务(每任务 4 次验证沙箱) | 32 到 36 秒,无基础设施失败 |
| 基线 10 任务 × k=2(agent 沙箱 + 验证沙箱各 20 个) | 37 秒 |
| 单次运行 agent 阶段(mock 三步) | 0.2 到 0.3 秒 |
| 端到端测试 `node/test-eval.py` | 16 项 |

mock 模型只会 `ls`、看题、提交,所以分数接近 0;它的作用是验证管线、计量、轨迹、噪声带(确定性 → δ = 0)和"同版本不被当作提升"。

**真实模型**:DeepSeek V4.1 Flash,API 里的模型名是 `deepseek-flash`,高峰价 $0.30 / $1.20 每百万 token(非高峰减半),代理按高峰价计费。密钥放在服务器 `./secrets/deepseek.key`(0600,仓库之外),以只读卷挂进代理容器,代理从 `UPSTREAM_KEY_FILE` 读,不进环境变量:`MODEL_BACKEND=upstream UPSTREAM_URL=https://api.deepseek.com/v1 UPSTREAM_KEY_FILE=/secrets/deepseek.key ./run-node.sh model`。第一次真实运行(py-fix-slugify):7 次调用、29 秒、38K 输入 / 4K 输出 token、0.016 美元,验收满分;agent 先读题和代码,写自己的检查脚本,改实现,再自测,然后提交。温度用服务商默认值,不用 0(见综述 2.3)。

CI 用 `SVC_SUFFIX=-ci PORT_OFFSET=1` 起自己的一套 fc-model-ci(mock)、fc-cp-ci、fc-eval-ci,不碰正在跑真实基线的那套。

### 17.3 踩到的坑

- 这个 rootfs 没有 apt 需要的 `/var/cache/apt/archives/partial`、`/var/log/apt`,构建层前要先建。
- 只装 `gcc` 没有 C 头文件(libc6-dev),验收里 `make` 失败;导入检查抓出来了,这正是它的用途。
- 评测器把检查结果写回任务集目录,而任务集在容器里是只读挂载 → 结果改到评测器状态目录;一个任务的异常不能让整批失败,否则正在跑的沙箱会漏销毁。
- 控制面两个并发创建同时写 `routes.json.tmp` 产生竞争(一个 500 并漏一个沙箱)→ 保存加锁。
- 沙箱里 `exec` 不是登录 shell,`/etc/profile.d` 不生效,评测器显式传 `PYTHONPATH=/opt`。
- curl `--data-binary` 默认是 POST,传文件到 `PUT /files` 要加 `-X PUT`。

### 17.4 没做的

真实模型的噪声基线(缺密钥);工作台无 xterm 和文件树;档案库与父代选择(第一版只做两版本比较);匹配预算基线、协议级反事实、篡改分类器;旧开发层 `L-9a360d46` 没有删除接口;CI 的 test-cp 步骤会重建 fc-cp 容器,期间工作台短暂不可用。

## 18. 分层镜像第一轮:裁剪内核、最小底座、层配方(2026-10-30)

对应 `archive/architecture-v0.2-2026-11-01.md`(现行见 `000`–`012`) v0.2 第 3 节。后端不变,仍是 Firecracker。

### 18.1 做了什么

| 件 | 位置 | 内容 |
|---|---|---|
| guest 内核 6.18.59 | `recipes/kernel-6.18/`:官方 `microvm-kernel-ci-x86_64-6.18.config` + `trim.fragment` + 容器内构建脚本 | 从 Firecracker 官方配置做减法:去模块、io_uring、user namespace、BPF 系统调用、kprobes、netfilter、网桥、IPv6、NFS、输入设备、休眠、SELinux/Smack/TOMOYO/AppArmor、调试信息;LSM 只留 landlock、lockdown、yama;补 EROFS(含 LZMA)。构建在一次性的 ubuntu:22.04 容器里,源码走 aliyun 镜像,16 线程几分钟 |
| 底座 L0+L1 | `recipes/base-debian-13/`:Dockerfile + build.sh | `debian:trixie-slim` + bash、util-linux、e2fsprogs、iproute2、tar、hostname、ca-certificates、python3;去文档、手册、locale、Python 测试目录;导出后装 fc-init、sandboxd、私有 shell、agent 与 aios 用户;mkfs 成 ext4(label sbx-base)并写清单(文件数、占用、sha256、配方路径) |
| 模板登记 | `recipes/templates/debian-13.json`,节点从 `TEMPLATES_DIR` 读 | 按需加载模式下内核和底座都从分块仓库取(jail 只绑定仓库挂载点),所以两者都 `imgstore push` 进仓库 |
| 层配方 | `node/build-layer.sh <recipe> [template] [parents]` + `recipes/dev-tools/` | 在指定模板上开构建沙箱,传入 `files/`,以 root 跑 `recipe.sh`,`pack` 成层,记录 `{layer, recipe_hash, template, parents}` 到 `state/layers-built.jsonl`。开发工具层改成这个配方(git、gcc、libc6-dev、make、jq、jinja2、pytest、`/opt/aios`);`build-dev-layer.sh` 保留为包装 |
| 验证 | `node/test-template.py <node> <template> [对照模板]`,进 CI | 冷启动、overlay 根、用户、Landlock ABI、io_uring 与 user namespace 关闭、无模块、EROFS、出口策略、底座不可写、pack、模板快照启动、按需加载实际拉取字节 |

### 18.2 数字(同一台机器,512 MiB VM)

| 项 | ubuntu-24.04 + 6.1(旧) | debian-13 + 6.18 裁剪(新) |
|---|---|---|
| vmlinux | 43 MB | 24 MB |
| 内核开启项 | 1238 | 1244(官方 6.18 为 1391) |
| 底座文件数 | 9264 | 3654 |
| 底座占用 | 417 MiB(1 GiB 盘) | 135 MiB(512 MiB 盘),清单记 118 MiB |
| Landlock ABI | 无 | 7(网络限制、unix socket 与信号作用域、审计) |
| io_uring / user namespace / 模块 | 开 | 关 |
| EROFS | 无 | 有 |
| 冷启动到首次 exec | 1.04 s | 1.04 s |
| 模板快照启动 | 0.16 s | 0.21 s |
| 首启按需拉取 | 约 9% 的 1 GiB | 约 100 MiB(python、libc、bash 两边都要碰) |
| 开发工具层 | 681 MiB(Ubuntu 包) | 在 debian-13 上重建一份,配方同一个 |

启动时间没变:路径被内核初始化、fc-init、sandboxd 的 Python 启动主导;要把这一段压下去得把 sandboxd 换成静态二进制(L1 的 Rust 化),那是下一步。真正落袋的是安全面(Landlock 7、三个攻击面关掉)、体积和文件数、以及"每一层都有配方和清单"。

产品层在新底座上端到端 16 项通过(`node/test-eval.py`),评测服务默认模板已切到 debian-13。

### 18.3 踩到的坑

- 精简镜像没有 CA 证书,apt 源用 https 会握手失败,配方里的镜像一律 http。
- `docker rm -q` 不存在。
- 按需加载模式下 jail 只看得到仓库挂载点,新内核不推进仓库就"找不到文件"。
- 会话重启会杀掉从 ssh 前台起的构建;长构建要用 `docker run -d` 或 nohup。
- `images/` 整体被 .gitignore,配方放在里面进不了仓库,移到 `recipes/`。

## 19. Rust 运行时替换 Python daemon(2026-10-30)

对应 `archive/architecture-v0.2-2026-11-01.md`(现行见 `000`–`012`) v0.2 第 4.2 节"Rust 基础运行时 + TS harness"的第一步:sandboxd 用 Rust 重写,协议逐项对等,底座去掉 python3。

### 19.1 实现

- `guest/sbx-runtime/`:424 行 Rust,依赖只有 libc、serde、serde_json、base64;AF_VSOCK 监听、只接受宿主机 CID;19 个操作(ping、health、exec、会话 open/exec/write/read/close、write_file、read_file、pack、list_dir、delete、set_time、add_entropy、net_config、quiesce、thaw、shutdown)与 `node/guest/sandboxd.py` 的请求、响应、流式事件格式一致,错误字符串保持 "类型: 信息" 的形状。用户查找直接读 `/etc/passwd`(静态链接下不走 NSS);降权顺序 setsid → setgroups → setgid → setuid,PTY 会话另加 TIOCSCTTY。
- `recipes/sbx-runtime/build.sh`:在 `rust:1-slim-trixie` 容器里 `+crt-static` 构建(glibc 与底座同版本),crates 走 rsproxy;产物 1.4 MB 静态二进制。首次编译 6 分钟,之后有缓存。
- 底座配方去掉 python3,`fc-init` 优先执行 `/usr/local/lib/sandboxd/sandboxd`,没有才回落到 Python 版。
- 节点加 `DEFAULT_TEMPLATE` 环境变量,现有套件不改就能整体跑在新模板上。

### 19.2 数字

| 项 | Python daemon(debian-13) | Rust 运行时(debian-13) |
|---|---|---|
| 底座文件数 | 3654 | 2953 |
| 底座占用 | 118 MiB | 96 MiB |
| daemon 体积 | python3 约 40 MB | 1.4 MB |
| 冷启动到首次 exec(首次,含按需拉取) | 1.04 s(拉 100 MiB) | 0.50 s(拉 79 MiB) |
| 冷启动(已缓存) | 1.00 s | 0.40 s |
| 模板快照启动 | 0.21 s | 0.08 s |
| pack 24 MiB | 0.05 s | 0.025 s |

启动时间减半,快照启动降到原来的四成:之前"路径被 Python 解释器启动主导"的判断是对的。

### 19.3 验证

`node/test-template.py` 12 项(探测改为纯 shell:securityfs 的 LSM 列表、io_uring sysctl 是否存在、unshare -U、/dev/tcp 直连被丢);全部节点套件跑在 Rust 运行时上(节点默认模板登记了 debian-13 就用它);开发工具层在无 python 的底座上重建(配方里的 python3-pytest 把 python3 装进层里);产品层端到端。

| 套件 | 结果 |
|---|---|
| test-node | 15 / 15 |
| test-layers | 8 / 8 |
| test-net | 15 / 15 |
| test-jail | 12 / 12 |
| test-snap(含 JAIL=0 的 uffd 段与排空重启) | 38 / 38 |
| test-template | 12 / 12 |
| test-eval(产品层,dev-tools 层在新底座上重建) | 16 / 16 |
| test-lazy | 7 / 7:冷启动 0.56 s 拉 106 MiB(底座的 20.6%),热启动 0.37 s,画像预取后只拉 22 MiB(4.2%),急切整包 1.48 s + 启动 |

套件本身改了三处才跑通,都是测试假设 guest 有 python3:guest 侧 HTTP 探测改为"有 curl 用 curl,否则 python3"(底座为此加了 curl,101 个文件、10 MiB);内存压力从 `bytearray` 改为 bash 变量装入 N MiB;哈希改用 `sha256sum /bin/bash`。

CI 镜像补了 gcc、libc6-dev、make(c-make-fix 任务的本地检查需要),单元测试在没有工具链的机器上整模块跳过。

顺带修了一个真 bug:JAIL=0 时建的模板快照文件是 root 0640,切回 jail 后非特权 VMM 读不了,而创建按键命中它就直接失败。现在模板键含 jail 标志,两种模式互不认;恢复失败时丢掉该快照并回退冷启动,创建不失败。

## 20. agent-harness 层与内核 ABI v1(2026-10-30 晚)

对应 `archive/architecture-v0.2-2026-11-01.md`(现行见 `000`–`012`) v0.2 第 3 节的五层:这一轮把 agent-harness 拆成独立层,给 agent-runtime 加上内核 ABI 和 Landlock 下放。

### 20.1 agent-harness 层

- 配方 `recipes/agent-harness/`:可重定位 CPython 3.13(python-build-standalone,Mac 下载后 scp,服务器连不上 GitHub)解到 `/aios/runtime/python`,离线装 jinja2;harness 代码到 `/aios/harness/aios`;启动器 `/aios/bin/aios`(设 PYTHONPATH、禁写字节码、用自带解释器跑 `aios.<sub>`)。全部 root 属主、a+rX,agent 用户只读。
- 层 510 MiB(runtime 255 MB、harness 56 KB),分块仓库按内容去重;python-build-standalone 的 install_only 包偏大,以后可换 stripped 变体。
- `dev-tools` 回归环境域:只装 git、gcc、libc6-dev、make、jq、python3、pytest,不再带 harness。
- 评测器:默认 agent 命令改为 `/aios/bin/aios run ...`;版本不显式给命令时不落库、运行时取当前默认(旧记录迁移);版本新增 `runtime`(底座里 sbx-runtime 的哈希)与 `harness_layer` 字段,Cairn 版本 = 两个哈希;`EVAL_LAYERS` 默认 = harness 层 + dev-tools 层。

### 20.2 内核 ABI v1(`guest/sbx-runtime/src/abi.rs`)

- unix socket `/run/aios/abi.sock`,SO_PEERCRED 只认 root / aios / agent;NDJSON;操作 hello、event、budget_get、budget_charge、checkpoint。
- 事件日志 `/run/aios/events.jsonl` root 0600:agent 只能追加(经 ABI),读不到;宿主经 sandboxd 的 `abi_events` 读,节点暴露 `GET /sandboxes/<id>/events`;本地预算上限经 `PUT /sandboxes/<id>/budget` 下发(代理侧的上限仍是权威)。
- harness 侧 `aios/abi.py`:socket 存在就在每次模型调用后 `budget_charge` 并记 `model_call` 事件,退出记 `exit` 事件;预算超限抛 BudgetExceeded(退出码 2);socket 不存在则全部为空操作,harness 在任何环境都能跑。
- 评测器每次运行结束从节点拉事件日志存进 run(最多 200 条),工作台和作弊信号以后都从这里取。

### 20.3 Landlock 下放(`src/landlock.rs`)

- exec 加 `landlock` 选项:`true` = 默认 harness 约束(只读全盘;可写 cwd、/tmp、home、/dev/null 等;TCP 只许连出口代理端口 3128;ABI ≥ 6 时附加 unix 抽象 socket 与信号作用域),或显式 `{ro, rw, connect_ports}`。在子进程 setsid 之后、降权之前施加,先 `PR_SET_NO_NEW_PRIVS` 再 `landlock_restrict_self`;内核 ABI 版本探测决定规则集结构体大小与可用权限位。
- 评测器对 agent 步骤默认开启(`EVAL_LANDLOCK=1`);验证步骤不需要。
- 这是 D4 "强制边界靠权限不靠约定"在 guest 内的第一块落地:harness 进程写不到 `/aios`、`/usr`,连不了代理以外的 TCP 端口。

### 20.4 解释器拆到 agent-langs 层

按"guest 同时有 Python 和 TS,默认 Python"的要求,解释器从 harness 层拆出来:`recipes/agent-langs/`(可重定位 CPython 3.13 + jinja2,bun 1.4 单文件;bun 也从 Mac 下载后 scp)装到 `/aios/runtime/{python,bun}`,`/aios/bin/{python3,bun}` 是软链;`recipes/agent-harness/` 只剩代码和两个启动器:`/aios/bin/aios`(默认,Python 种子)与 `/aios/bin/aios-ts`(bun 跑 `/aios/harness/ts/<sub>.ts`,目前占位)。评测器默认层 = langs + harness + dev-tools。好处:RSI 每个候选只重建几十 KB 的 harness 层;TS 与 Python harness 可以并存比较。

### 20.5 结果与坑

| 项 | 结果 |
|---|---|
| `test-template`(加 ABI 与 Landlock 共 5 项) | 17 / 17 |
| `test-eval`(加"运行记录含 harness 经 ABI 上报的事件") | 17 / 17,一次运行记到 model_call 与 exit 事件 |
| 冷启动 / 模板快照启动 | 0.43 到 0.47 s / 0.09 s(ABI 线程不影响) |
| 运行时二进制 | 1.46 MB |

CI 侧另有两个坑:提交时把 agent-langs 的解释器二进制(约 180 MB)带进了仓库,已从索引移除但历史里留着(不做强推);`.gitignore` 里未锚定的 `bin/`、`images/` 把任务集参考解里的 `reference/bin/`、`sorted/images/` 也忽略了,新克隆缺文件导致 CI 的导入检查对 organize-files、cron-backup-script 失败,工作树里有所以本地从未发现——模式一律锚定到仓库根。

CI 运行 16(提交 7e0d29d)全绿,全部套件跑在 debian-13 + Rust 运行时上:node 15、net 15、layers 8、jail 12、snap 38、cp 20、eval 17、template 18、lazy 7,单元测试与配方检查通过。

坑:Landlock 对非目录路径(如 `/dev/null`)只接受文件类权限位,带目录权限会 EINVAL 让整个规则集失败,进而让 exec 报错、评测里的 agent 步骤全变成基础设施失败;按 fstat 区分文件与目录后解决。显式策略要始终放行 `/dev/null`,shell 重定向离不开它。harness 层在加 ABI 客户端之前建过一次,忘了重建就看不到事件:层是制品,代码改了就得重建。
