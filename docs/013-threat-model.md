# 013 威胁模型

[009](009-security-and-data.md) 说了边界在哪里、数据归谁。这一页把它整理成一份可以拿给外人审的威胁模型:资产、信任边界、攻击者、每条边界上的控制与**已知未做**、残余风险、审的人该看哪里。写于 2026-11-03(D45),控制面在 Go、八个上下文落地之后;改了边界就改这一页。

## 资产

| 资产 | 在哪 | 丢了意味着 |
| --- | --- | --- |
| 宿主与其他租户的沙箱 | 节点容器、每 VM 的 jail | 逃逸 = 全盘 |
| 模型上游密钥、代码托管令牌、注入用的凭证 | 密钥目录(只读挂进模型代理 / Agent 服务)、节点的凭证保险库(`state/credentials.json` 0600) | 别人花我们的钱、以我们的名义写代码 |
| 控制面令牌(部署者)与成员令牌 | 密钥目录;成员令牌只存哈希 | 冒充 |
| 用户的仓库、工单文本、记忆、轨迹 | 节点可写层与 bundle(fc-store)、Agent 服务的 Store、评测器状态 | 泄露、篡改 |
| 账单与用量 | Postgres `usage_events` / `meter_statements` | 少算、多算 |
| 评测真值与接受门 | 评测器存储;验收沙箱 | 作弊进版本 |
| 审计日志 | 各服务 `audit.jsonl` | 事后说不清 |

## 信任边界与攻击者

```
 外部 agent / 人 ──HTTP(bearer)──▶ 控制面 ──HTTP(node token)──▶ 节点代理 ──vsock──▶ sandboxd ──exec──▶ 沙箱内进程(agent / 用户代码)
                                      │                              │                                   │
                                      ▼                              ▼                                   ▼
                                Redis / Postgres / fc-store     tap + nftables + 出口代理             guest 内核(无模块、无 io_uring、无 userns)
```

| 攻击者 | 位置 | 想要 |
| --- | --- | --- |
| **A1 沙箱内的代码**(agent 被注入、用户跑了恶意包) | guest 用户态 | 逃出 VM;连不该连的地方;偷同宿主别的沙箱;花别人的预算;污染评测 |
| **A2 恶意或被盗的成员令牌** | 控制面 API | 看、改、删别的项目的沙箱 / 卷 / 账单;放宽出口;耗尽配额 |
| **A3 被攻破的节点** | 节点容器 / 宿主 | 读所有沙箱、伪造用量、拿节点上的凭证保险库 |
| **A4 网络上的人**(同一内网) | 控制面与节点之间、工作台与控制面之间 | 窃听令牌、改请求 |
| **A5 供应链**(OCI 镜像、任务集、依赖) | 层、题集、pip/npm 缓存 | 把恶意代码带进沙箱或评测 |

部署者(拿 cp.token、能登上宿主)**不在模型里**:他就是信任根。

## 每条边界上的控制,与没做的

### 沙箱 → 宿主(A1)

| 控制 | 状态 |
| --- | --- |
| Firecracker + jailer:每 VM chroot、非特权 uid、独立 cgroup(内存、CPU、pids) | 有;jail=1 在 CI 的 test-jail 里逐项验 |
| guest 内核裁过:无模块、无 io_uring、无 user namespace;sandboxd 用 seccomp,起的进程 Landlock + no_new_privs | 有 |
| VMM 异常退出 → 沙箱标 failed、审计、释放 | 有 |
| 设备面最小:virtio-blk / net / vsock / balloon(/ pmem) | 有 |
| 节点容器本身是特权的(SYS_ADMIN、SYS_PTRACE、关 AppArmor / seccomp) | **已知**:逃出 VM 的人直接在一个特权容器里;补法是宿主级 AppArmor / seccomp 给 jailer 进程(没做),或节点跑在专用机器上 |
| 内存:VM 之间经 pmem/DAX 共享的只是只读 base 的页缓存 | 有;写时各自私有 |
| 侧信道(同宿主 cache timing 等) | **不防**;要的话专用机器 |

### 沙箱 → 网络(A1)

| 控制 | 状态 |
| --- | --- |
| guest 只有一条到宿主的 tap,无 DNS;nftables 只放 3128(出口代理)与 established,其余 drop | 有;test-net / test-egress |
| 出口代理按策略:closed / allowlist / audited,L7 规则,限额,每次决定审计 | 有 |
| 凭证注入加在明文 HTTP 的头上,值不进沙箱 | 有;HTTPS 不做中间人(D36)。对 HTTPS 接口用 `upstream: "https"`(D89):沙箱对该主机发明文,代理加凭证后自己发起 TLS 并校验证书;沙箱到代理这段明文只在宿主的 tap 上。**没写这种规则的 HTTPS 请求仍然注入不了、也看不到路径**——这是选择,不是漏洞 |
| 沙箱之间不互通(各自 /30) | 有 |
| 端口暴露:guest 服务只能经节点 → 控制面带 bearer 访问 | 有;**已知**:谁能看哪台的端口只看沙箱归属,没有按端口的策略 |
| 放宽出口要 owner 批准 | 有(D42) |

### API 调用者 → 控制面(A2)

| 控制 | 状态 |
| --- | --- |
| bearer;成员令牌只存哈希;成员只见本账户的项目(沙箱 404 而非 403,不泄露存在) | 有 |
| owner / member 两级;配额(并发、存储、每日花费)在创建时查 | 有 |
| 节点、迁移、价目表、凭证保险库、层分发、导入只给部署者 | 有 |
| 令牌可轮换、可设过期(D46);**命名 API 密钥**绑单项目、可只读、可撤销(D56) | 有;细粒度操作范围、来源 IP 限制没做 |
| 控制面不做 TLS,端口只绑 127.0.0.1,靠前面的反向代理 | **已知**;deploy/README 写了 |
| 请求体上限(默认 512 MiB)与每令牌每分钟请求数(默认 600) | 有(D46);并发上限没做 |
| 审计含操作者,成哈希链,`GET /audit/verify` 找断裂处 | 有(D46);**仍是本地文件**,挡不住整文件重写,独立存储没做 |
| 滥用:一键停用账户(暂停其沙箱、成员只读)、检测器标记持续满载与出站洪泛 | 有(D53、D58);检测只标记不处置;注册风控与试用额度没做 |

### 控制面 ↔ 节点(A3、A4)

| 控制 | 状态 |
| --- | --- |
| 节点令牌(文件,控制面只读挂);每节点一个 | 有 |
| 节点联系不上控制面时自我围栏(暂停 / 销毁自己的沙箱),控制面可选地在围栏窗口之后自动接管 | 有(D54),默认关;**没有在真实网络分区上演练过** |
| 节点报的用量就是账单的依据 | **已知**:被攻破的节点可以伪造用量、也能读它上面所有沙箱的盘;节点是信任边界内的组件,防的是"节点被攻破"之前那一步,即 A1 |
| 内网明文 HTTP | **已知**;同机 docker 网络时可接受,跨机要加 TLS 或 WireGuard |
| 节点上的凭证保险库 0600,值不进审计 | 有 |

### 供应链(A5)

| 控制 | 状态 |
| --- | --- |
| OCI 镜像成层:base 赢(镜像的 /bin、/etc/passwd、libc 不覆盖我们的);ENTRYPOINT 不执行 | 有(D37/D41) |
| 题集来源签名、版本化、导入检查 | 有 |
| 共享缓存只读、键带租户前缀 | 有 |
| 层与模板的内容哈希 | 层 id 随机、bundle 内容寻址;**层没有签名** |

## 残余风险(按严重度;2026-11-03 晚 D46 补了 2、3、4 的一半:令牌可轮换可过期,控制面有体积与速率上限,审计成哈希链——各条后面标了)

1. **VM 逃逸落在特权节点容器里**。缓解:专用节点机器;宿主级 AppArmor/seccomp 给 jailer;把节点容器需要的能力降到最小(SYS_PTRACE 只为 core-sched cookie,可以去掉换一个核调度策略)。
2. **长期令牌**。缓解:令牌过期与轮换(**已做**:rotate / expiry)、范围令牌(**已做**:API 密钥只许某项目、可只读,D56;按操作的范围没做)。
3. **控制面无限速、无体积上限**。缓解:每令牌每分钟请求数(**已做**,默认 600)与请求体上限(**已做**,默认 512 MiB);并发上限没做。
4. **审计可被本机 root 改写**。缓解:哈希链(**已做**:每行 prev + h,`GET /audit/verify` 找到第一处断裂)、转发到独立存储(没做——链只能发现篡改,挡不住整文件重写)。
5. **节点自报用量**。缓解:控制面抽查(exec 一个计数、对照 cgroup 的读数)。

## 外人该看哪里

`guest/sbx-runtime/src/`(seccomp、Landlock、drop_privs)、`node/fcapi.py`(jailer 参数、设备面)、`node/netmgr.py` + `node/egress_proxy.py`(nftables 与代理决定)、`cp/internal/tenancy` + `httpapi/server.go` 的 `canSee` / `projectFor` / `memberPaths`(谁能看谁)、`cp/internal/network/requests.go`(放宽的定义)、`node/Dockerfile` 与 `run-node.sh`(节点容器要的能力)。真机套件 `node/test-jail.py`、`test-net.py`、`test-egress.py`、`test-isolation`(tests/unit)是现成的检查点。

## 落地清单(2026-11-03;每一条要么指向证据,要么写明没有)

这是给"能不能对陌生租户开放"做的逐条对照。**证据**一栏是能重跑的东西;"没有"就是没有。

| # | 威胁 | 缓解 | 证据 | 缺口(对外开放前要补) |
| --- | --- | --- | --- | --- |
| T1 | A1 逃出 VM | jailer、裁过的 guest 内核、seccomp / Landlock | CI `test-jail`;guest 有单测 | **没有外部渗透测试**;节点容器是特权的(残余风险 1);同宿主侧信道不防 |
| T2 | A1 连不该连的网 | nftables + L7 出口代理 + 审批流 | `test-net`、`test-egress`、`tests/unit/test_egress_policy.py`、Go 的 network 测试 | HTTPS 不看路径(选择);无 DNS 外泄的检测 |
| T3 | A1 滥用算力 | 沙箱 cgroup 上限;D58 检测;D53 停用 | Go `abuse_test.go`、`TestSuspendingAnAccount…`;节点指标由 CI 的 test-cp 验 | 检测不自动处置;没有按账户聚合;没有注册风控 |
| T4 | A2 越权访问别的项目 | 项目隔离、404 不泄露存在、密钥绑单项目 | `TestQuotasAdmit…`、`TestAPIKeys…`、`TestSuspending…` | 没有对 API 面做模糊测试 |
| T5 | A2 令牌泄漏 | 哈希存储、过期、轮换、撤销、`cairn_` 前缀 | `tenancy_test.go`(过期、范围) | 没有泄漏扫描回调(GitHub secret scanning 之类) |
| T6 | A2 拖垮控制面 | 请求体与速率上限 | `TestUsersAndAuth` 一带的速率用例 | 没有并发上限;没有按账户的限速 |
| T7 | A3 节点被攻破 | 节点令牌、节点 = 信任域内 | — | 节点自报用量无独立校验(残余风险 5) |
| T8 | A4 内网窃听 | 端口只绑 127.0.0.1,前面放反代;控制面 ↔ 节点可选 TLS(D62) | `deploy/README.md`;节点 HTTPS 与控制面信任 CA 的单测 | TLS 默认关、证书手工轮换、**没在真实跨机环境验证**;节点到 fc-store 等其它跳仍明文 |
| T9 | A5 供应链 | base 赢、ENTRYPOINT 不执行、内容寻址 bundle | D37 / D41 的测试 | **层没有签名**;无镜像漏洞扫描 |
| T10 | 数据丢失 | 备份手册、故障演练 | `operations.md` 的演练记录(Postgres 还原:11 表一致;Garage 还原:1004 个块逐个核哈希一致,`deploy/garage/restore-drill.sh`) | 本地目录后端 `store/` 的还原没演练过;Garage 单节点无副本 |
| T11 | 审计抵赖 | 哈希链 | `internal/audit` 的测试 | 日志在本机,整文件重写发现不了 |
| T12 | 脑裂(同一沙箱跑两处) | 节点自我围栏 + 控制面窗口(D54) | `test_a_fenced_node_pauses…`、`TestAutoEvacuateWaits…` | **没有真实网络分区演练** |

**对外开放前的最短清单**:T1(请外部做一轮渗透)、T8(在真实跨机环境把 TLS 打开并验证)、T12(找一台能断网的机器演练一次围栏)、T10(多副本 Garage;`store/` 还原)。其余是加固,不是开门条件。
