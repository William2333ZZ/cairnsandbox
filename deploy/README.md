# 一台机器上的整套(docker compose)

`run-node.sh` 一条条起的服务,这里一次起齐:注册表(Redis)、SQL(Postgres)、分块仓库、节点、控制面(含工作台与 E2B 口)、模型代理、评测器、Agent 服务。两种起法共用镜像、状态目录与网络,可以混用。

## 前提

- Linux 宿主,`/dev/kvm`、`/dev/vhost-vsock`、`/dev/net/tun`、`/dev/fuse` 可用;Docker 24+ 与 compose v2。
- 镜像:在仓库根 `docker build -t fc-node:dev -f node/Dockerfile .` 与 `DOCKER_BUILDKIT=1 docker build -t fc-cp:dev cp/`。
- 内核与基础镜像在 `images/`(`node/build-base.sh`),语言层 / harness 层按 [010 部署](../docs/010-deployment.md) 的"构建层与模板"建一次,把层 id 填进 `.env` 的 `EVAL_LAYERS` / `INTAKE_LAYERS`。

## 第一次

```bash
cd deploy
cp .env.example .env            # 改 REPO_DIR / DATA_DIR / 预算;要真模型就填 MODEL_BACKEND=upstream 与 UPSTREAM_URL
./make-secrets.sh               # secrets/cp.token、secrets/db.password(0600;别的密钥——deepseek.key、gitea-cairn-bot.token——自己放进去)
../console/build.sh         # 控制台的静态文件:Node 只在构建镜像里,生成仓库根的 console-dist/,控制面在 /console/ 托管
docker compose up -d
curl -s -H "Authorization: Bearer $(cat secrets/cp.token)" http://127.0.0.1:18700/health
```

工作台在 `http://127.0.0.1:18700/ui/`,令牌是 `secrets/cp.token`;E2B 形状的口在 18710(`X-API-Key` 同一个令牌)。SDK:`pip install -e ../sdk/python`,`CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=$(cat secrets/cp.token) python3 ../sdk/python/examples/quickstart.py`。

端口都只绑 127.0.0.1;对外暴露放一个反向代理在前面(TLS、限速),控制面自己不做 TLS。

## 热启

`.env` 的 `TEMPLATE_POOL`(默认一条:512 MiB / 1 vCPU,常备 1 台)让节点启动时为这些规格建模板快照并预热,之后同规格的创建从快照起(约 85 ms,冷启约 400 ms;见 [benchmark](../docs/research/benchmark-2026-11.md))。多加几条就是多几种常用规格;`ready` 是常备的已启动台数,占内存,按机器定。

## 升级

节点换版本不丢沙箱:

```bash
docker build -t fc-node:dev -f ../node/Dockerfile ..            # 新镜像
curl -X POST -H "Authorization: Bearer $T" http://127.0.0.1:18700/nodes/fc-node/drain    # 多节点:先排空(persistent 迁走);单节点:跳过,靠下面的 SIGTERM
docker compose up -d fc-node                                    # 停旧容器时节点收到 SIGTERM,把运行中的 persistent 沙箱暂停到盘(stop_grace_period 120 s),起来后恢复它们
curl -X POST -H "Authorization: Bearer $T" http://127.0.0.1:18700/nodes/fc-node/uncordon
```

控制面无状态(状态在 Redis / Postgres / 节点):`docker compose up -d fc-cp` 直接换;多副本时再起一个 `fc-cp-b`(复制 service,改 `CP_REPLICA` 与端口)就是滚动。模型代理 / 评测器 / Agent 服务同样直接换;Agent 服务重启时进行中的派工按 010 的"恢复"续跑。

Postgres 大版本升级走 `pg_dump` / 导入;Redis 的 appendonly 文件随 `registry-data/` 走。

## 出问题时

故障场景(节点失联、Redis / Postgres / fc-store 不可达或丢数据、磁盘满、令牌泄露)与各自的处置、指标与 Prometheus 告警规则([alerts.yml](alerts.yml))、容量规划,在 [运维手册](../docs/guide/operations.md)。

## 备份

要备的只有"不能从节点重建"的:`db-data/`(账户、项目、用量、结算、卷与构建记录)、`store/`(bundle 与层:durable 沙箱的归档)、`secrets/`。`registry-data/`(路由、节点、租约)丢了控制面会从节点对账重建;各 `*-state/` 是服务自己的工作目录,丢了就是丢了那部分历史(评测批次、派工记录),按需要备。

```bash
docker compose exec fc-db pg_dump -U cp cp | gzip > backup/cp-$(date +%F).sql.gz
tar czf backup/store-$(date +%F).tgz store/
```

## 单机多容器模拟多机

`./multinode.sh up N` 在一台机器上起 N 个节点容器加一个控制面、注册表、数据库和分块仓库(单独的 compose 项目 `cairn-multi`,和平常的 `docker compose up` 互不干扰)。每个节点有自己的网络命名空间、状态目录(`state`、`state-2`、…)、端口和令牌,对控制面来说就是 N 台独立的机器。

```bash
cd deploy
PORT_OFFSET=1000 MEM_BUDGET_MIB=1024 ./multinode.sh up 3     # 3 个节点;端口整体加 1000,避开本机已有的栈
./multinode.sh status                                         # 控制面眼里的节点:存活、VM 数、内存
./multinode.sh kill 3        # 节点 3 断电(SIGKILL,没有"先暂停再退出")
./multinode.sh evacuate 3    # 把它的 durable 沙箱从最近一次暂停的 bundle 在别的节点恢复
./multinode.sh start 3       # 节点回来;控制面对账,删掉它手里已经搬走的旧副本
./multinode.sh partition 3   # 断网但进程还活着(`heal 3` 接回去)
./multinode.sh down -v
```

可调:`MEM_BUDGET_MIB`(**每个节点**的内存预算;不设就取本机内存的一半除以节点数,每个至多 8192)、`MAX_VMS`、`STORE_BW_MBPS`(给共享的分块仓库限速,模拟机器之间的链路;影响 bundle 迁移和镜像按需读取)、`FENCE_AFTER_S` 与 `AUTO_EVACUATE_AFTER_S`(自我围栏与自动接管,见 `.env.example`)。节点 `k` 的宿主端口是节点 1 的端口加 `k-1`。脚本先用 `docker compose config` 展开 `docker-compose.yml` 再复制节点,所以你在 `.env` 或 compose 文件里改过的镜像名、端口、预算都会带上。

**在一台机器上实测过的**(3 个节点,每个最多 2 台 VM、1 GiB,2026-11-05,在一份干净检出上):4 台 256 MiB 沙箱分到三个节点(2/1/1);对一台 durable 沙箱写文件后暂停,`kill` 它所在的节点,控制面把节点标为不可用;`evacuate` 把它从 bundle(拉取约 60 MiB)在另一个节点恢复,文件内容还在;`start` 之后节点回到 0 台 VM(旧副本被对账删掉);`partition` 后节点被标为不可用,`heal` 后恢复。

**这种模拟不能验证的**:跨机的网络带宽和延迟(这里多一跳只有毫秒级;可以用 `STORE_BW_MBPS` 限仓库,但限不了节点之间的直连)、几个节点共用同一台机器的内存和磁盘带来的互相挤占、真正的网络分区与丢包、时钟偏差、跨机的页缓存行为。这里也**没有做过压力测试**:共享的开发机上别人也在跑任务,所以只用了每节点 2 台 VM 的规模。注意:节点目录是容器里的 root 写的,清理要用容器(`docker run --rm --entrypoint rm -v "$PWD:/d" fc-node:dev -rf /d/state-2`)。

## 多台真机

一台**控制面机器**(控制面、注册表、数据库、分块仓库,也可以自带一个节点)加任意多台**节点机器**(只跑节点和微虚机)。加一台机器就是:在那台机器上起一个节点,再告诉控制面它的 IP 和令牌;没有集群组建步骤,别的节点也不用改配置。完整的英文步骤见 [docs/en/multi-machine.md](../docs/en/multi-machine.md),这里是摘要。

**网络**:控制面 → 每个节点的 8800(控制面调用节点的全部 API,头里带令牌);每个节点 → 控制面机器上分块仓库的 18900(按需读内核和基础镜像、迁移和接管用的 bundle、模板快照)。节点之间不通信(迁移是"导出到仓库、从仓库导入")。**分块仓库没有任何鉴权**,里面有沙箱的内存快照:所有机器放在私网里(VPC、WireGuard),两个端口只绑私网地址并用防火墙限定到集群内的机器,绝不要把 18900 或 8800 暴露在公网网卡上。节点 API 有令牌但默认是明文 HTTP;要 TLS 见 `deploy/tls/gen-certs.sh`(D62),`node.sh` 没接这一项,也没验证过。

**控制面机器**:在 `.env` 里加 `STORE_BIND=<本机私网 IP>`(仓库的绑定地址,默认只绑 127.0.0.1),照常 `docker compose up -d`。

**每台节点机器**:Docker、`/dev/kvm` 等设备(和任何节点一样)、本仓库的检出、`fc-node` 镜像,以及控制面机器 `images/layers/upper-template.ext4` 这一个小文件(512 MiB 的稀疏文件,用 `rsync -S`);**不用**自己构建内核和基础镜像,节点从仓库读。

```bash
docker build -t fc-node:dev -f node/Dockerfile .
rsync -S 控制面机器:cairn/images/layers/upper-template.ext4 images/layers/
cd deploy
./node.sh init fc-node-b 10.0.0.2 http://10.0.0.1:18900     # 节点名、本机私网 IP、仓库地址
./node.sh up
```

`init` 写出 `deploy/node.env`(0600,里面有新生成的令牌)。然后在控制面机器上以 admin 登记这台节点(令牌取自节点机器的 `node.env`):

```bash
curl -fsS -X POST http://127.0.0.1:18700/nodes -H "Authorization: Bearer $(cat secrets/cp.token)" -H 'Content-Type: application/json' \
  -d '{"name":"fc-node-b","url":"http://10.0.0.2:8800","token":"<node.env 里的 NODE_TOKEN>"}'
```

登记写进注册表,每个控制面副本下一次轮询就认识它。之后:创建时 `"node":"fc-node-b"` 钉住、`POST /sandboxes/<id>/migrate {"to":"fc-node-b"}` 迁移、`POST /nodes/fc-node-b/evacuate` 接管挂掉节点上的 durable 沙箱。`FENCE_AFTER_S`(节点)与 `AUTO_EVACUATE_AFTER_S`(控制面,须大于节点的 `FENCE_AFTER_S` 加 30 秒)配合处理失联;各机器时钟要同步。

**验证到哪一步**:这些文件(`docker-compose.node.yml`、`node.sh`、登记请求)在**一台**机器上跑过:两个独立的 compose 项目只通过 Docker 网桥地址 172.17.0.1 互相访问(把它当作"私网 IP"),第二份检出的 `images/` 里只有 `upper-template.ext4`。覆盖了:按 IP 登记、在远端节点创建并执行、经仓库在线迁移(256 MiB 暂停沙箱,总共 2.6 秒,拉取 60 MiB)、杀掉节点后 evacuate、文件内容完好。**没有**覆盖:两台物理机、真实网络延迟、防火墙、控制面与节点之间的 TLS、控制面前面的反向代理。先在单机上用上面的 `multinode.sh` 排练过再上真机更稳妥。

## 和 run-node.sh 的差别

- 密钥经 compose secrets 文件挂进容器(`CP_TOKEN_FILE`、`CP_DB_PASSWORD_FILE`),不进环境变量与 `docker inspect`;run-node.sh 用的是环境变量。两种起法读的是同一份 `cp.token`。
- compose 的 `NODES` 固定在文件里;run-node.sh 按参数拼。
- 网络默认由 compose 建(`fcnet`);机器上已经有 run-node.sh 建的,就 `NETWORK_EXTERNAL=true`。
