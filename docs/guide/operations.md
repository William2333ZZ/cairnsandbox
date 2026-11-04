# 运维手册:故障、指标、容量

给部署和值班的人。设计理由在 [003](../003-sandbox.md)、[D31](../decisions.md)(多副本与共享状态)、[D34](../decisions.md)(检查点);部署的起步在 [deploy/README](../../deploy/README.md);威胁与残余风险在 [013](../013-threat-model.md)。

**每条结论后面标了它的依据**:**〔测试〕** 有自动化测试在跑(CI 或单测);**〔演练〕** 在开发机上实际做过一遍;**〔读代码〕** 只是读代码得出,没有故障注入证明。别把〔读代码〕当成保证。

## 0. 先看哪里

| 看什么 | 怎么看 | 回答什么 |
| --- | --- | --- |
| `GET /health` | 无需令牌 | 副本名、注册表是否可达(`registry.ok`)、每个节点 `alive` / `last_err` / 空闲内存 |
| `GET /metrics`(admin) | Prometheus 文本 | 依赖是否可达、节点负载、沙箱按类别计数、5xx 与限流计数、计量心跳;规则在 [deploy/alerts.yml](../../deploy/alerts.yml) |
| `GET /audit?n=200`(admin 看全部,成员看本账户) | JSON,新的在前 | 控制面做过的每件事:`node_down`、`node_up`、`registry_error`、`reconcile_adopt`、`reconcile_delete_stale`、`evacuate`、`migrate`、`preempt`、`member_rotate` … |
| `GET /audit/verify`(admin) | JSON | 这个副本的审计日志哈希链是否完整 |
| 节点的审计 | `docker logs fc-node`(每行一条 JSON) | 节点上发生的:`destroy`、`recover`、`orphan_removed`、`export_error`、`drain_*`、`layer_*` |
| 控制台的节点页 | admin | 每个节点的存活、负载、cordon |

## 1. 谁保存什么

| 组件 | 保存 | 丢了能重建吗 |
| --- | --- | --- |
| **节点**(fc-node) | 沙箱本身:内存、盘、检查点、卷、层、模板快照;运行态的权威 | 否(这是数据本体) |
| **注册表**(Redis `fc-registry`) | 路由(沙箱 → 节点、项目、优先级、元数据)、节点登记、对账租约 | **能**:路由由节点对账重建(见 F5) |
| **数据库**(Postgres `fc-db`) | 账户、项目、成员(令牌哈希)、配额、默认网络、放宽请求、用量事件、账单与结算、卷与构建记录 | **不能**,靠备份(见第 4 节) |
| **分块仓库**(`fc-store`) | durable 沙箱每次暂停发布的 bundle、层与模板快照的归档、按需加载的镜像块 | 不能;丢了就不能接管与分发,节点本地的不受影响 |
| **控制面**(`fc-cp`) | 无状态;本地只有 `audit.jsonl`(每副本一份)与(文件注册表模式下)`routes.json` | 是 |
| 模型代理 / 评测器 / Agent 服务 | 各自状态目录:模型用量计数、评测批次、派工记录 | 否,按需备份 |

## 2. 故障手册

每节:**现象 → 影响 → 会自己恢复吗 → 你要做什么**。

### F1 单台沙箱 `failed`

VMM 异常退出或健康看门线程发现 guest 失联时,节点把它标 failed,下一个 TTL 周期(1 秒)销毁并清理 jail、cgroup 与 tap,审计写 `destroy` 与原因。控制面下次对账(≤ 1 分钟)删掉它的路由。〔读代码;`test-jail` 验清理〕

- 你要做:没有。durable 的沙箱若有过暂停,从最后一份 bundle 能再导入(`POST /sandboxes/import`);运行中没暂停过的状态已经丢了。

### F2 节点优雅重启(升级)

`docker compose up -d fc-node` 或 `docker stop`(SIGTERM,等 120 秒):节点先 **cordon 自己**不再接新活,persistent 的**暂停到盘**,ephemeral 的**销毁**,然后退出;新容器起来后 `recover()` 把盘上有暂停快照的沙箱恢复成 paused,第一次请求时透明恢复。〔CI 的 test-snap 与 test-net 的重启恢复〕

- ephemeral 沙箱会被销毁——评测器等调用方要能重跑。
- 多节点时先 `POST /nodes/<n>/drain`(persistent 迁走),再换镜像,再 `uncordon`:零中断。

### F3 节点失联(宿主崩溃、容器被杀、网络断)

- **现象**:控制面 2 秒轮询一次;失败即审计 `node_down`,`cairn_node_up{node} == 0`;列表里它的沙箱显示 `unreachable`;对这些沙箱的请求回 **503 "node X is down"**(durable 的带 `durable: true`);新建避开它。〔测试:test-cp 的节点失联一节;Go 的 httpapi 测试〕
- **默认不会自动接管**。有意的:网络分区时节点可能还活着,两处同时跑同一台沙箱、写同一份 bundle。**可选的自动接管(D54)**靠"节点自我围栏"保证安全:节点设 `FENCE_AFTER_S=120`(控制面静默超过这么久,节点自己把 persistent 沙箱暂停——durable 的会发布 bundle——其余销毁,health 里 `fenced: true`),控制面设 `AUTO_EVACUATE_AFTER_S=180`(**必须大于**节点的围栏时间再加余量)后,每次失联只自动 evacuate 一次,审计 `auto_evacuate`。两个都是 0 即关。边界:节点既连不上控制面又连不上 fc-store 时,围栏暂停成功但 bundle 没发布,接管用的是更早的一份;围栏只防"网络断",防不了"控制面自己出问题而节点没事"(那种情况下节点也会被围栏,所以别把窗口设得比控制面重启时间还短)。〔测试:节点 `fence_all`、控制面 `AutoEvacuateOnce`;**没有**在真实网络分区上演练过〕
- **你要做**:
  1. 确认节点**真的死了**(宿主关机、容器已停、网络不是只对控制面断);不确定就先等,或让它先 `cordon`。
  2. `POST /nodes/<n>/evacuate`:durable 的沙箱从**最后一份发布的 bundle**在别的活节点恢复成 paused(丢的是那次暂停之后的状态);非 durable 的算 `lost`;**卷不带**(在死节点的盘上);一次只有一个副本做(租约)。响应里 `moved` / `lost` 各列出是谁。〔测试:test-cp〕
  3. 节点修好回来:`recover()` 恢复它盘上暂停的沙箱;控制面对账发现路由已指向别处的陈旧副本,**删掉旧副本**(审计 `reconcile_delete_stale`),所以接管过的沙箱不会出现两份。〔测试〕
- 告警:`AiosNodeDown`、`AiosSandboxesStranded`。

### 控制面 ↔ 节点上 TLS(D62)

默认是明文 HTTP(同机 docker 网络可接受)。跨机必须开 TLS:①`deploy/tls/gen-certs.sh <目录> <节点主机名>…` 生成 CA 和每个节点的证书(私钥只在你给的目录里);②节点设 `TLS_CERT` / `TLS_KEY`(compose:`NODE_TLS_CERT` / `NODE_TLS_KEY`),它就在原端口讲 HTTPS;③控制面设 `NODE_CA_FILE=<ca.crt>`,节点用 `https://…` 的 URL 登记(`NODES` 或 `POST /nodes`);④先加新节点验证,再把旧节点换成 https。证书一年到期:**到期的节点会被当作不可达**(告警 `AiosNodeDown`),所以到期前换证——控制面要重启才会重读 CA(换 CA 才需要,换节点证书不用)。边界:`/health` 等公开路由也走 TLS;控制面到节点的 WebSocket 隧道同样校验;节点到 fc-store、到模型代理等其它内网跳**没有**开 TLS。〔测试:节点 HTTPS 单测、控制面对 https 节点的单测;**没有**在真实跨机部署上验证〕

### F4 控制面副本挂了或重启

- 控制面**无状态**(路由与节点登记在 Redis,租户在 Postgres),重启不丢任何东西;重启期间请求连接被拒,SDK 报 `ApiError(0, transport …)`。〔演练:本次部署反复重启生产副本,路由与会话保持〕
- 多副本时,前面要**自己放负载均衡**(我们不带);工作台只指一个副本。
- 计量只在持租约的副本上跑(每分钟一次)。副本全挂超过 1 小时,那段空档**不回补**(避免对未知时段多收费);1 小时以内的空档会按实际间隔补记。〔读代码 `Tick`:`dt > 3600` 置 0〕
- 重启后节点对账在 30 秒到 1 分钟内完成;期间新创建的沙箱路由已经写进了注册表,不受影响。

### F5 注册表(Redis)

- **不可达**:所有按沙箱 id 的请求回 **503 "registry unavailable"**(不是 404——路由多半还在)。创建时沙箱已经建在节点上、但路由写不进去:审计记 `registry_error`,**沙箱仍然存在**,注册表恢复后一分钟内对账会把它收养回来。`/health` 里 `registry.ok` 为 false。〔测试:`TestWhenTheRegistryIsUnreachable…`〕
- **数据丢失**(空 Redis 起来、`registry-data/` 被清):路由由**节点**重建——每台节点上线时与此后每分钟,控制面把节点上有而注册表没有的沙箱收养回来(审计 `reconcile_adopt`)。**项目、优先级、E2B 元数据**从节点上存的标签读回,所以账单与配额不会记错账。〔测试:`TestALostRegistryIsRebuilt…`〕**〔演练:2026-11-03 在开发机上真删了我自己一台沙箱在 Redis 里的路由——删后按 id 的请求 404;55 秒后路由带着 demo 项目、`batch` 优先级与元数据原样回来,与删除前逐字段一致〕**。收养在节点上线时与之后每分钟发生,所以丢失到重建之间**最长约一分钟**按沙箱 id 的请求会 404(此时注册表是可达的、只是没有这条路由)
  - 例外:**在这个标签机制上线之前创建的沙箱**节点上没有标签,收养后落在 **default 项目**(账单与配额记到 default)。运维可以事后在数据库里对,不对的重建没有办法知道。
  - 节点登记:环境变量 `NODES` 里的会在控制面启动时重新登记;只靠 `POST /nodes` 加过的要**重新加**。
  - 租约自动重来;不丢租户数据(那在 Postgres)。
- 告警:`AiosRegistryDown`。

### F6 数据库(Postgres)

- **不可达**:创建 **503 "tenancy store unavailable"**(不是 404 "no project");**成员令牌验证失败 → 401 "bad token"**(验证要查库;注意是 401 不是 503);**部署者令牌照常可用**(在内存里);已存在沙箱的 exec、文件、暂停、恢复、列表**不依赖库**,照常。〔测试:`TestWhenTheTenancyDatabaseIsDown…`〕计量停,恢复后继续,空档规则同 F4。
- **数据丢失**:从备份还原(第 4 节)。**沙箱与路由不受影响**(在节点与 Redis)。备份之后新增的账户、成员、用量、结算、卷与构建记录丢了;备份之后发的**成员令牌失效**,要重新发;卷的记录丢了但卷文件还在节点上。〔读代码;还原本身见演练〕
- 告警:`AiosDatabaseDown`、`AiosMeteringStalled`。

### F7 分块仓库(fc-store)

- **不可达**:运行中的沙箱**不受影响**。durable 沙箱**暂停时发布 bundle 失败**(节点审计 `export_error`;暂停本身成功)——这次暂停之后它就没有可接管的最新 bundle,别指望 evacuate 能救它。新建沙箱读基础镜像与层经 lazyfs:本地缓存里有的块照常,**没缓存的块每块最多等 120 秒然后读失败**,guest 看到块设备 I/O 错误。迁移、接管、模板与层的分发、bundle 导入都会失败。〔读代码;**没有故障注入测试**〕
- **数据丢失**:bundle 丢了 = durable 沙箱不再能接管(节点本地的仍在);层与模板快照在各节点本地还有一份(只丢跨节点的副本);按需加载的基础镜像要重新推。
- 告警:`AiosStoreDown`。

**存储后端(D52)**:`fc-store` 有两种后端——本地目录(默认,`store/`)和 S3 兼容桶(`fc-s3`,Garage)。对节点和控制面的 HTTP 接口完全一样,换后端不用改任何调用方。
- 启用:`deploy/garage/bootstrap.sh` 起 `fc-s3` 并建桶与密钥(密钥只写 `$SECRETS/s3.env`,0600,不进 git、不打印);`imgstore.py migrate` 把现有 `store/` 拷进桶(幂等,先块后清单);然后 `STORE_BACKEND=s3 ./run-node.sh store`。
- **回退**:不带 `STORE_BACKEND` 重启 `fc-store` 即回到本地目录;迁移后本地目录原样保留,直到你确认可以删。
- 后端出错时 `fc-store` 回 **502**(不是 404),调用方不会把"存储不可达"当成"没有这个块"。
- 备份对象变成:`s3/`(Garage 的元数据与数据目录)+ `secrets/s3.env`。**别直接拷在线的 `s3/meta/db.sqlite`**:先 `docker exec fc-s3 /garage meta snapshot` 得到一致的快照,再拷快照 + `meta/` 下所有小文件(**`cluster_layout`、`data_layout`、`node_key`、`peer_list`** 缺了还原出来的节点会显示"no role assigned"并对所有请求回 503)+ 整个 `data/`。`deploy/garage/restore-drill.sh` 把这套做成了脚本并**演练过**:在另一个容器里起拷贝,把 1004 个块经 S3 读回并逐个核哈希,全部一致(Garage 的文件属 root,脚本用容器来拷和删);单节点 Garage 没有副本,**它和本地目录一样是单点**,多副本要加节点并把 `replication_factor` 改成 3(见 `deploy/garage/garage.toml`)。

### F8 模型代理

沙箱里 agent 的模型调用失败(402 / 502),**沙箱本身不受影响**。计量读的是代理的累计用量,恢复后下一个 tick 补上差值,不丢。〔读代码〕

### F9 磁盘满

| 哪里 | 现象 |
| --- | --- |
| 节点状态盘 | 创建、检查点、暂停失败;节点审计报错 |
| 控制面状态盘 | **审计写不进去时请求不报错、审计静默缺行**(写入错误被忽略)——所以要监控这块盘 |
| fc-store 盘 | bundle 发布失败(同 F7) |
| Postgres 盘 | 写失败,创建 503,计量停 |

**两个会自己长大的东西**:`audit.jsonl` 没有自动轮转(每副本一份,只追加);`usage_events` 没有保留期。轮转审计:停副本、`mv audit.jsonl audit-<月>.jsonl`、起副本(新文件的链从空开始;旧文件用 `audit.Verify` 单独核对)。

### F10 令牌泄露

- 成员令牌:`POST /projects/<p>/members/<u>/rotate`(owner、admin 或本人),旧令牌**立刻**失效;在审计里查 `by` 看用它做过什么。
- 部署者令牌:换 `secrets/cp.token`,**重启所有读它的服务**(控制面、评测器、Agent 服务、节点页的人);之后核一遍 `/audit` 里的 admin 操作。

### F12 滥用:停用账户

**发现**:控制面自带一个只观察的检测器(D58),把持续满载 CPU(默认平均 ≥ 分到的 vCPU 的 90%、10 分钟)和异常出站标出来:告警 `AiosAbuseSignal`,`GET /abuse/signals` 看是谁。正常的构建也会满载,所以它**不会自动处置**。

**处置**:发现某个账户在滥用(挖矿、扫描、出口洪泛、违反条款):`POST /accounts/<a>/suspend {"reason": "…"}`([API](api/tenancy.md))。它先记下停用,再把该账户所有运行中的沙箱**暂停**(状态保留,留着调查),此后账户成员只能看列表与单台信息、删沙箱;读文件 / 事件 / 端口也不行(会让节点把沙箱恢复)。取证:审计里按 `project` 过滤、`GET /sandboxes/<id>/network/decisions` 看出口决定、用部署者令牌进沙箱看现场(部署者不受停用限制)。处理完 `POST /accounts/<a>/unsuspend`,被暂停的沙箱由 owner 自己恢复。多副本:停用状态存在 Postgres,各副本最多 3 秒后都认。

## 3. 指标与告警

**仪表盘**:`deploy/grafana/cairn-overview.json` 是现成的 Grafana 总览(节点在线、运行中沙箱、依赖可达性、停用账户与滥用信号、节点内存与负载、沙箱状态、失联节点上的沙箱、响应与 5xx、计量停滞、待批放宽),导入时选你的 Prometheus 数据源;`tests/unit/test_alerts.py` 保证它只查 `/metrics` 真有的指标。

`GET /metrics`(admin)。规则在 [deploy/alerts.yml](../../deploy/alerts.yml),单测保证里面引用的指标都真实存在。

| 指标 | 标签 | 含义 | 对应告警(阈值) |
| --- | --- | --- | --- |
| `cairn_node_up` | node | 1 = 最近一次轮询成功 | AiosNodeDown(0 持续 1 分钟,critical) |
| `cairn_node_vms_total` / `_running` | node | 节点上的沙箱数 / 运行中 | — |
| `cairn_node_mem_mib_running` / `cairn_node_mem_budget_mib` | node | 已用 / 预算内存 | AiosNodeMemoryHigh(> 90% 持续 10 分钟) |
| `cairn_node_load` | node | 空位占用与内存占用里大的那个(0–1) | AiosNodeLoadHigh(> 0.85 持续 10 分钟);AiosNoRoomAnywhere(所有节点都 > 0.85,critical) |
| `cairn_nodes_alive` / `cairn_nodes_total` | — | 活节点数 / 登记数 | — |
| `cairnboxes` | class, priority, node, project | 路由表里的沙箱数 | — |
| `cairnboxes_on_down_nodes` | — | 路由指向失联节点的沙箱数 | AiosSandboxesStranded(> 0 持续 2 分钟,critical) |
| `cairn_dependency_up` | dep = registry / database / store | 1 = 刚探测过可达 | AiosRegistryDown、AiosDatabaseDown(critical);AiosStoreDown(warning)(各持续 1–2 分钟) |
| `cairn_metering_last_tick_age_seconds` | — | 距上次记账多少秒 | AiosMeteringStalled(> 300 持续 5 分钟) |
| `cairn_http_responses_total` | class = 2xx / 3xx / 4xx / 5xx | 本副本启动以来的响应计数 | AiosServerErrors(5xx 占比 > 5% 且 > 0.1 次/秒,持续 10 分钟) |
| `cairn_http_rate_limited_total` | — | 被每令牌限速(429)的次数 | AiosRateLimiting(info) |
| `cairn_network_requests_pending` | — | 等 owner 批的出口放宽数 | AiosEgressRequestsWaiting(info,一天) |

没有指标、要用 cron 的:**审计链**——`curl -H "Authorization: Bearer $T" $CP/audit/verify`,`ok` 不是 true 就报警;**备份是否按时做了**——看备份文件的时间。

## 4. 备份与恢复

**要备的**:`db-data/`(Postgres,用 `pg_dump`)、`store/`(fc-store 的目录)、`secrets/`。**不用备**:Redis(F5 会重建)、控制面状态目录(除审计,有合规要求再备)、节点上的沙箱(它们是数据本体,备份的办法是 durable + bundle)。

```bash
docker compose exec -T fc-db pg_dump -U cp cp | gzip > backup/cp-$(date +%F).sql.gz
tar czf backup/store-$(date +%F).tgz store/
```

**还原演练**(别等出事才第一次做):

```bash
gunzip -c backup/cp-<日期>.sql.gz > /tmp/restore.sql
docker run -d --name fc-db-drill --memory 256m -e POSTGRES_USER=cp -e POSTGRES_DB=cp -e POSTGRES_PASSWORD=drill postgres:16-alpine
until docker exec fc-db-drill pg_isready -U cp; do sleep 1; done
docker exec -i fc-db-drill psql -q -U cp cp < /tmp/restore.sql
# 逐表对行数:accounts projects members quotas usage_events meter_statements volumes builds network_requests project_network prices
docker rm -f fc-db-drill
```

**2026-11-03 在开发机上做过一遍**(生产库,只读 `pg_dump`,临时容器 256 MiB,用完即删):

| | 结果 |
| --- | --- |
| 备份 | `pg_dump` 79 KB,1 秒 |
| 还原到空库 | 1 秒,0 个错误 |
| 逐表对行数 | accounts 2 / projects 3 / members 2 / quotas 1 / usage_events 534 / network_requests 1 / project_network 1 …… 共 11 张表,源与还原**全部一致** |

这是**小库**上的结果;库大了以后要重新量(`usage_events` 没有保留期,见第 5 节)。`store/`(本地目录后端)的还原没有演练过;Garage 的还原已演练(见上节)。

## 5. 容量规划

**每台节点**(起点,不是保证):

| 项 | 怎么定 |
| --- | --- |
| `MEM_BUDGET_MIB` | 准入按各沙箱**配置内存之和**对预算。宿主给这个节点留的内存 × 0.8。实测空闲沙箱(128 MiB 配置)占 60–72 MiB,真负载的沙箱会更接近配置值,保守按"配置值 + 40 MiB"算 |
| `MAX_VMS` | 受 tap、cgroup、文件句柄限制;共享机器上默认 32。专用机器上的上限还没量(见 [benchmark](../research/benchmark-2026-11.md)) |
| 给人留的余量 | `reserve_vms` / `reserve_mem_mib`(默认 2 台 / 1 GiB):batch 与 background 只能用余量之外的,interactive 才能用满 |
| 节点状态盘 | 每台沙箱的可写层是稀疏文件(规格里 `disk` 默认 512 MiB),实际占用看 `disk_used_mib`;再加层、模板快照、检查点(每个检查点 = 一份可写层拷贝 + 一层内存) |

**集群**:

- **N+1**:最大那台节点挂了,剩下节点的空闲位要装得下它上面的 persistent 沙箱;否则 evacuate 会失败一部分。
- 负载持续 > 0.85 就加节点;`AiosNoRoomAnywhere` 是最后一道。
- **fc-store**:每个 durable 沙箱每次暂停发布一份 bundle(内存 Diff + 可写层,共享层按内容去重)。起点:存储 ≥ 2 × Σ(durable 沙箱的内存 + 已用盘)。
- **Postgres**:`usage_events` 每台运行中沙箱每分钟约 3–4 行(vcpu、内存、盘,有出站和模型 token 再加),没有保留期。100 台长期运行 ≈ 每天 50 万行,几十 MB;按季度看一次表大小,到了要自己归档(`meter_statements` 已结算的期间可以把更早的明细归档掉)。
- **审计**:每个副本一份,增长看操作量,没有轮转(F9)。

## 5b. 重建基础镜像(并排,不动现有沙箱)

基础镜像(`recipes/base-debian-13`)的指纹在每个模板快照的键里,**原地替换会让所有模板快照和暂停的沙箱作废**。所以永远并排建一个新的,再切换。D76 做过一次,步骤:

1. 编译运行时:`recipes/sbx-runtime/build.sh images/bin-out-v2`(rust 容器,几十秒)。
2. 建基础镜像:`SANDBOXD_BIN=$PWD/images/bin-out-v2/sandboxd recipes/base-debian-13/build.sh images/base-v2`,把结果复制成 `images/base/base-debian-13-v2.ext4`(**文件名带版本**,存储里按文件名取)。
3. 推进存储:`python3 -c "import sys; sys.path.insert(0,'node'); import imgstore; imgstore.push_remote('http://127.0.0.1:18900','base-debian-13-v2.ext4','images/base/base-debian-13-v2.ext4')"`。
4. 登记模板:新增 `recipes/templates/debian-13-v2.json`(内核同旧的,`base` 指向新文件),重启节点(`LAZY_STORE=http://fc-store:8900 ./run-node.sh`,会优雅暂停再恢复运行中的沙箱)。此时 `debian-13`(旧)和 `debian-13-v2` 并存,已有沙箱和快照不受影响。
5. 验证:用 `template: "debian-13-v2"` 建沙箱,看 `getent hosts localhost`;在 `mem: 1024` 的沙箱里写 800 MB 再 `pack`;用 `BASE_TEMPLATE=debian-13-v2 python3 ops/dogfood-devloop.py prepare …` 重做开发模板并跑 Go 测试。
6. **切换**(需要决定,不可热回退的只有"重做模板"):给节点设 `DEFAULT_TEMPLATE=debian-13-v2`;评估器、Agent 服务用的 `EVAL_TEMPLATE` / `INTAKE_TEMPLATE` 同改;把要保留的模板在新基础上重做。旧的 `debian-13` 等没有沙箱再用它时才删。

回退:把 `DEFAULT_TEMPLATE` 改回 `debian-13` 并重启节点,旧基础上的快照仍在。

## 6. 值班速查

| 症状 | 先查 |
| --- | --- |
| 请求 503 "node X is down" | F3;节点页、`cairn_node_up` |
| 请求 503 "registry unavailable" | F5;`/health` 的 `registry.ok`、Redis 容器 |
| 创建 503 "tenancy store unavailable" / 成员全 401 | F6;Postgres 容器 |
| 创建 429 "no node with capacity" | 第 5 节;`cairn_node_load`、配额(429 "quota: …" 是项目配额,不是容量) |
| 创建 402 "quota: daily budget" | 项目当天花费到了上限,控制台 → 项目与成员 改配额 |
| 沙箱列表里一堆 `unreachable` | F3 |
| 账单突然不动 | `cairn_metering_last_tick_age_seconds`;F6 |
| 某个成员说"令牌突然不行了" | 是否过期(`expiry`/`rotate` 的 ttl)、是否被限速、F6 |
