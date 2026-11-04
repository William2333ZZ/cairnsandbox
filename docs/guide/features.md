# 功能清单

数字来自一台开发机上的单机实测,不是对外基线;完整的没做清单见 [ROADMAP](../../ROADMAP.md)。

| | 有 | 数字(开发机,[bench](../research/benchmark-2026-11.md)) |
| --- | --- | --- |
| 沙箱 | Firecracker microVM + jailer;冷启 / 从模板快照热启;空闲自动暂停、透明恢复;TTL 与暂停保留期;ephemeral / persistent 两类,interactive / batch / background 三级优先与抢占 | 冷启 396 ms,热启 **85 ms**,exec 往返 5 ms,16 台不变慢 |
| 时光 | 运行中打检查点、原地回滚(id / 网络不变)、从检查点 fork n 台、运行中提交为模板 | 检查点 47 ms,回滚 238 ms,fork 328 ms |
| 搬家 | 迁移(带检查点与卷)、节点失联后从 bundle 接管、排空、均衡、cordon | |
| 文件系统 | 只读 base + 层(配方 / OCI 镜像 / 运行中打包)+ 可写层;按需加载的分块仓库;独立卷(单写者 / 多读者) | 每台空闲 60–72 MiB(含 guest 页) |
| 网络 | guest 无 DNS、一切经出口代理;closed / allowlist / audited;L7 规则(host 通配 / 正则、path、method)、凭证注入(密钥不进沙箱)、每沙箱限额;放宽要 owner 批准;端口暴露(含 WebSocket 隧道) | |
| 租户 | 账户 ⊃ 项目 ⊃ 成员;配额(并发、存储、每日花费);项目默认网络;令牌轮换与过期;命名 API 密钥(单项目、可只读、可撤销);邀请码自助开户;预付额度;账户停用与滥用检测(只标记);成员只见本账户 | |
| 计量 | CPU 秒、内存 MiB 秒、存储、出站字节、模型 token;价目表版本化;结算账本与 CSV | |
| 运维 | 控制台(Next.js,`/console/`)含实时指标曲线;公开 `/status`;节点自我围栏与可选自动接管;预览域名;控制面 ↔ 节点可选 TLS;分块仓库可落到 S3 兼容存储(Garage);控制面多副本(Redis / Postgres 共享状态,节点是运行态权威);`/metrics`;审计哈希链;compose 一键部署;升级 drain → 换镜像 → uncordon | |
| agent 面 | 沙箱内核 ABI:预算、工单、inbox、事件日志、report;harness 目录;评测与进化 | |
