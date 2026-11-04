# 上生产:反向代理、域名、环境变量与上线清单

写给要把沙箱云从"一台机器上 compose 跑起来"推到"对外服务"的人。设计理由见 [013 威胁模型](../013-threat-model.md) 末尾的清单和 [商业化差距](../research/commercialization-gap-2026-11.md);日常运维见 [运维手册](operations.md)。**这页里标"没验证"的地方是我们没在真实多机 / 公网环境走过的。**

## 1. 拓扑

```
 用户 ──HTTPS──▶ 反向代理(Caddy / nginx)──▶ 控制面 :18700(API、控制台 /console/、/status)
                         │                  └▶ E2B 口 :18710
                         └─ *.preview.example.com ─▶ 控制面 :18700(按 Host 改写成 /sandboxes/<id>/ports/<port>)
 控制面 ──HTTPS(可选)──▶ 节点 :8800(每台机器一个)──▶ microVM
 控制面 ──▶ Redis(路由)  Postgres(租户、账本、用量)  fc-store(块,可落 Garage S3)
```

控制面和节点的端口默认只绑 127.0.0.1 / 内网;**对外只开反向代理的 443**。

## 2. Caddy 示例(自动 HTTPS)

```caddyfile
api.example.com {
    reverse_proxy 127.0.0.1:18700 {
        flush_interval -1          # SSE(/events/stream)与流式 exec 不能被缓冲
    }
}
e2b.example.com {
    reverse_proxy 127.0.0.1:18710
}
# 预览域名:通配证书需要 DNS 验证(Caddy 的 DNS 插件,或你自己的证书)
*.preview.example.com {
    tls { dns <你的 DNS 服务商插件> {env.DNS_TOKEN} }
    reverse_proxy 127.0.0.1:18700 {
        flush_interval -1
    }
}
```
控制面设:`PREVIEW_DOMAIN=preview.example.com`、`CP_TRUST_PROXY=1`(只在它前面确实有你自己的反向代理时才设;用于注册接口按来源地址限速)。WebSocket 升级头 Caddy 默认透传。〔没验证:公网域名上的预览与 cookie 行为(`Secure` 靠 `X-Forwarded-Proto` 判断,Caddy 会带)〕

## 3. 环境变量总表(控制面)

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `CP_TOKEN` / `CP_TOKEN_FILE` | 部署者令牌 | 无(= 不鉴权,**只限开发**) |
| `CP_REGISTRY` | `redis://…` 多副本共享路由 | 文件(单副本) |
| `CP_DB` | `postgres://…`;空 = SQLite | SQLite |
| `NODES` | 节点列表 JSON;也可 `POST /nodes` | `[]` |
| `NODE_CA_FILE` | 校验 https 节点的 CA(D62) | 系统根证书 |
| `STORE` | fc-store 地址 | 空(无跨节点状态) |
| `CP_MAX_BODY_MIB` / `CP_RATE_PER_MIN` | 请求体上限 / 每令牌每分钟请求数 | 512 / 600 |
| `CP_AUDIT_EXEC` | `0` = 审计链里不记 exec 与文件读写(D80;默认记,命令行涂黑并截 200 字符) | 1 |
| `CP_TRUST_PROXY` | 相信 `X-Forwarded-For`(D56 后修正) | 0 |
| `PREVIEW_DOMAIN` | 预览域名(D55) | 空(关闭) |
| `AUTO_EVACUATE_AFTER_S` | 节点失联多久后自动接管(D54);**必须大于节点的 `FENCE_AFTER_S` + 30** | 0(关) |
| `ABUSE_CPU_PCT` / `ABUSE_EGRESS_BYTES_PS` / `ABUSE_WINDOW_S` / `ABUSE_SCAN_S` | 滥用检测阈值(D58);CPU 0 = 关 | 90 / 0 / 600 / 60 |
| `CONSOLE_DIR` | 控制台静态文件目录 | `/console` |

节点:`TLS_CERT` / `TLS_KEY`(HTTPS)、`FENCE_AFTER_S`(自我围栏)、`MAX_VMS`、`MEM_BUDGET_MIB`、`METRICS_EVERY_S` / `METRICS_POINTS`、`JAIL`(生产必须 1)。分块仓库:`STORE_BACKEND=s3` 与 `S3_*`(见 [运维手册](operations.md) 的存储后端)。

## 4. 上线前清单(按重要性)

**必须**
1. `CP_TOKEN` 设了,`secrets/` 目录权限 0700、不在 git 里。
2. 节点 `JAIL=1`;节点所在机器是**专用**的(VM 逃逸落在特权节点容器里,见 013 T1)。
3. 反向代理上了 TLS;控制面 / 节点 / Redis / Postgres 的端口没有绑公网。
4. 控制面 ↔ 节点跨机时开 TLS(`deploy/tls/gen-certs.sh`,D62),并检查证书到期提醒。
5. Postgres 与 Redis 有备份并**演练过还原**(Postgres 与 Garage 已演练,`deploy/garage/restore-drill.sh`;本地目录的 `store/` 没演练过)。
6. 告警规则([alerts.yml](../../deploy/alerts.yml))接到有人看的地方,至少 `AiosNodeDown`、`AiosStoreDown`、`AiosServerErrors`、`AiosAbuseSignal`。

**开放给陌生人之前**
7. 找人做一轮外部渗透测试(013 T1);修完再开。
8. 在一台能断网的机器上演练节点围栏与自动接管(D54),确认窗口设置合理后再把 `AUTO_EVACUATE_AFTER_S` 打开。
9. 价目表按真实成本定(见 [定价对照](../research/pricing-2026-11.md)),账户默认预付(D59),邀请码带试用额度与配额(D60)。
10. 状态页放到**独立于控制面**的地方(控制面挂了它不能一起挂):静态页轮询 `/status`,或用第三方探活服务按状态码(503 = 中断)判断。
11. 写 SLA / 隐私 / 数据处理条款(这不是工程问题,但没有它不该收钱)。

## 5. 依赖与漏洞(D73)

上线前和之后每月手工跑一遍(盒子出口受限,没放进 CI):

```bash
cd cp && go run golang.org/x/vuln/cmd/govulncheck@latest ./...        # 控制面;有 "affected by" 就升级对应模块并重建镜像
cd console && npm audit --omit=dev; cd ../sdk/js && npm audit         # 前端与 JS SDK
```
**在沙箱里跑控制面的扫描**(D78,盒子上不用装 Go):`MEM=4096 python3 ops/dogfood-egress.py <源码tar> dev-go`(内部用 `template_key` + `boot:"cold"` + `mem` 按 4 GiB 冷启动模板的层)——沙箱出口默认全关,首次会被拒,按失败里的域名(`goproxy.cn`、`vuln.go.dev`)申请放行并批准,两轮后出结果。内存要 4 GiB(峰值约 1.9 GB,2 GiB 的开发模板会被 OOM 杀掉,退出码 137)。

基础镜像(`alpine:3.20`、`ubuntu:22.04`、`golang:*-alpine`)也要定期重新拉取并重建。

## 6. 升级与回滚

见 [deploy/README 的"升级"](../../deploy/README.md):节点收到 SIGTERM 会把运行中的 persistent 沙箱暂停到盘再退出,新容器起来后恢复;控制面无状态,直接换;多副本滚动。回滚 = 换回旧镜像标签。数据库只加表加列(幂等迁移),旧版本控制面能在新库上跑(忽略新表)。
