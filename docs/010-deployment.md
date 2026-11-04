# 010 部署与运维

本文说明怎样把这套系统跑起来、更新、排障。它建立在 [003](003-sandbox.md) 到 [009](009-security-and-data.md) 之上。

## 跑在哪

一台共享的 Ubuntu 开发机(192 核,8 张 4090,别人也在用),Docker 可用,无 kvm 组,出网受限。仓库在 `<repo>`,Gitea 在同机 Docker(`fc-gitea`),CI 由 `fc-runner`(act_runner)执行。Mac 上有一份代码镜像,用于编辑与下载外网资源(python-build-standalone、bun),再 scp 上去。

规矩:不做压测;不重启 dockerd,不碰别人的容器;服务只绑 `127.0.0.1`;长构建用 `nohup` 或 `docker run -d`,会话重启会杀前台作业;不改宿主 sysctl。

## 一次起齐(compose)

`deploy/` 下的 compose 文件把下面每个服务一次起齐,与 `run-node.sh` 一一对应、可以混用;第一次、升级(节点 drain → 换镜像 → uncordon,SIGTERM 时暂停到盘)、备份(只备 db-data、store、secrets)与第二个节点写在 [deploy/README.md](../deploy/README.md)(D44)。

## 服务

全部由 `run-node.sh` 启动,都在 Docker 网络 `fcnet` 上,状态目录在仓库下。

| 服务 | 容器 | 端口(本机) | 状态目录 | 作用 |
| --- | --- | --- | --- | --- |
| 节点代理 | fc-node(`NODE=n` → fc-node-n) | 18800 + n | `state[-n]/` | 沙箱 |
| 分块仓库 | fc-store | 18900 | `store/` 或 S3 桶 | 层、bundle、内核、基础镜像;`STORE_BACKEND=s3` 时落到 `fc-s3`(Garage,`deploy/garage/bootstrap.sh`,D52) |
| 控制面(E2B 形状) | fc-cp | 18710 | — | 同一个进程的第二个端口(CP_E2B_PORT),E2B SDK 的 REST 形状,`X-API-Key` 鉴权;CP_E2B_DEFAULT_TEMPLATE 指默认模板名 |
| 控制面 | fc-cp | 18700 | `cp-state/` | 放置、路由、`/ui/`、`/eval/*`;Go 镜像 `fc-cp:dev`(`docker build -t fc-cp:dev cp/`);`CP_NAME=fc-cp-b CP_HOST_PORT=18710 ./run-node.sh cp 0` 再起一个副本 |
| 数据库 | fc-db | 15432 | `db-data/` | 副本共享的 Postgres(`./run-node.sh db`,密码在密钥目录 `db.password`):账户、项目、成员、配额、用量事件、价目表;fc-db 在跑时 `run-node.sh cp` 默认 `CP_DB=postgres://cp:…@fc-db:5432/cp`,`CP_DB=sqlite` 回到 `cp-state/cp.sqlite`;CI 用 SQLite |
| 注册表 | fc-registry | 16379 | `registry-data/` | 副本共享的 Redis(`./run-node.sh registry`);fc-registry 在跑时 `run-node.sh cp` 默认 `CP_REGISTRY=redis://fc-registry:6379`,`CP_REGISTRY=file` 回到文件;CI 的 `-ci` 栈用文件 |
| 模型代理 | fc-model | 18500 | `model-state/` | 令牌、预算、计量 |
| 评测器 | fc-eval | 18600 | `eval-state/` | 任务集、版本、批次 |
| Agent 服务(旧名接单层) | fc-intake | 18400 | `intake-state/` | Conversation、Connector、Router、Eval、Ticket、Routine、Approval、Memory、Relay、Watchdog;`GITEA_REPO=William2333ZZ/cairn ./run-node.sh intake` |

```bash
./run-node.sh store
./run-node.sh                                # 节点 fc-node,JAIL=1,LAZY_STORE=http://fc-store:8900
./run-node.sh cp 0
MODEL_BACKEND=upstream UPSTREAM_URL=https://api.deepseek.com/v1 UPSTREAM_KEY_FILE=/secrets/deepseek.key ./run-node.sh model
./run-node.sh eval
```

`SVC_SUFFIX=-ci PORT_OFFSET=1` 起 CI 自己的一套 fc-model-ci(mock)、fc-cp-ci、fc-eval-ci,不碰正在用的那套。

节点容器需要 `/dev/kvm`、`/dev/net/tun`、`/dev/vhost-vsock`、`/dev/fuse`,NET_ADMIN、SYS_ADMIN、SYS_PTRACE、SYS_NICE,私有 cgroup 命名空间,关闭默认 AppArmor 与 seccomp。节点环境变量:`DEFAULT_TEMPLATE`、`MAX_VMS`、`MEM_BUDGET_MIB`、`JAIL`、`LAZY_STORE`、`TEMPLATE_POOL`、`TEMPLATES_DIR`。

## 密钥

`chat.secret`:聊天工具调 webhook 时的共享密钥(CHAT_SECRET_FILE=/secrets/chat.secret);出站地址与格式用 CHAT_WEBHOOK_URL、CHAT_FORMAT(generic | feishu | slack)传给 `run-node.sh intake`。`cp-state/users.json`:部署者在设置页新建的用户,只有令牌哈希。模型账户:`UPSTREAM_KEY_FILES='{"eval":"/secrets/deepseek-eval.key"}'` 让评测与进化用自己的上游 key,不和 live 的 agent 共用余额(D28);没配就共用,`fc-model` 的 `/health` 写明每个账户用的是自己的还是共享的。`cp.token`:控制面、Agent 服务、评测器共用的 bearer,`run-node.sh` 第一次启动时在密钥目录生成,之后每次启动都读它;工作台登录页要的就是这个文件的内容;机上的脚本调服务要带 `Authorization: Bearer $(cat $SECRETS_DIR/cp.token)`。评测器只在 `EVAL_REQUIRE_TOKEN=1` 时检查。

目录 `./secrets/`,文件 0600,以只读卷挂进需要的容器:`deepseek.key`(模型上游)、Gitea agent 账号令牌(接单层)。Gitea 管理员凭证在 `/path/to/gitea-admin-credentials.txt`。节点令牌在 `state*/node-token`。它们不出现在环境变量、`docker inspect`、仓库、文档里。

## 构建层与模板

| 产物 | 命令 | 产出 |
| --- | --- | --- |
| guest 内核 | `recipes/kernel-6.18/build.sh` | `images/kernel-out/vmlinux-6.18.59-sbx`,推进分块仓库 |
| runtime 二进制 | `recipes/sbx-runtime/build.sh` | `images/bin-out/sandboxd`(静态,约 1.5 MB) |
| 基础镜像 | `recipes/base-debian-13/build.sh` | `images/base/base-debian-13.ext4` 与清单,推进分块仓库 |
| 层 | `node/build-layer.sh <recipe> [template] [parents]` | 层 id 写到 `state/<recipe>-layer-id.<template>`,记录到 `state/layers-built.jsonl` |

层配方按序构建:`agent-langs` → `agent-harness` → `dev-tools`。配方在 `recipes/<name>/recipe.sh`,附带文件在 `recipes/<name>/files/`(不进仓库,大二进制从 Mac scp)。构建在一台临时沙箱里以 root 运行配方,然后 pack。评测器的 `EVAL_LAYERS` 默认取三个层 id 文件。

模板登记在 `recipes/templates/<name>.json`,节点从 `TEMPLATES_DIR` 读;按需加载模式下内核与基础镜像都必须在分块仓库里。

## 沙箱云控制台

`console/` 是独立的 Next.js 应用(D51),静态导出到 `console-dist/`,由控制面只读挂载并在 `/console/` 托管。构建:`console/build.sh`(Docker,Node 工具链只在构建镜像里);改了前端只需重建并重启 `fc-cp`,不用重新构建控制面镜像的其它部分。CI 跑 `docker build --target check`(类型检查 + 单测)。`console-dist/` 不进 git。

## 升级

- 改了 harness 或配方,重建对应层并重启评测器以刷新默认层;层是制品,不重建就还是旧的。
- 改了 runtime,重建二进制、重建基础镜像、重推仓库、重启节点;模板快照失效,首启冷启动后重建。
- 改了节点代码,重建 `fc-node:dev` 镜像;改了控制面(`cp/`),`docker build -t fc-cp:dev cp/`(构建里跑 go vet 与 go test,不过就没有镜像)。`run-node.sh` 会先 `docker stop`(排空)再重建容器。
- 改了内核,重建并推仓库,改模板 JSON,重启节点。

## CI

Gitea Actions,`.gitea/workflows/ci.yml`,`fc-runner` 挂 docker socket、接入 `fcnet`。机器出不去,所以自己 `git clone http://fc-gitea:3000/...` 检出。步骤:lint(编译、shell 语法、单元测试)→ 构建 `fc-ci:dev` → 起 CI 自己的服务栈 → 端到端套件:node、net、layers、jail、snap(含 JAIL=0 的 uffd 段与排空重启)、cp(自起 fc-node-2、fc-node-3)、template、lazy、eval → 配方解析检查。

每个端到端脚本退出时打印 `RESULT ok=N fail=M`;CI 同时要求退出码 0、日志里有 `RESULT`、没有 `FAIL`。"没有 FAIL"不等于通过。

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 沙箱创建 429 | 节点满 | 控制面换节点;单节点时等或调 `MAX_VMS` |
| 模板快照启动失败后冷启动 | 快照在另一 jail 模式下建的 | 正常回退;模板键含 jail 标志 |
| 按需加载"找不到文件" | 内核或基础镜像没推进仓库 | `imgstore push` |
| uffd 不可用 | jail 丢掉 CAP_SYS_PTRACE,或宿主 sysctl 为 0 | 节点回退到合并文件;`/health` 的 `uffd_note` 说明 |
| apt 在沙箱里失败 | 精简镜像缺 `/var/cache/apt/archives/partial`、`/var/log/apt`;https 源无 CA | 配方先 mkdir;源用 http |
| exec 全部基础设施失败 | Landlock 规则集 EINVAL(非目录路径带目录权限) | 已按 fstat 区分;显式策略放行 `/dev/null` |
| 事件日志里没有 harness 事件 | harness 层在加 ABI 客户端前建的 | 重建层 |
| 评测写不进任务集目录 | 任务集是只读挂载 | 结果写评测器状态目录 |
| 控制面 500 且漏沙箱 | 并发写路由文件 | 已加锁 |
| 新克隆缺任务参考文件 | `.gitignore` 未锚定的 `bin/`、`images/` | 模式一律锚定到仓库根 |
| 磁盘满 | 可写层 512 MiB | 规格 `disk` |
| 排空没跑 | `docker rm -f` 是 SIGKILL | 先 `docker stop` |
| 稀疏内存快照恢复后 guest 三重错误 | 分块传输把洞填成零 | 清单带数据区段,只写区段内字节 |
| CI 全绿但某步没跑 | `cd` 失败被 `&&` 吞掉,读到上一步日志 | 绝对路径;每步先删日志;要求 RESULT 行 |

## 跑一轮版本循环

```bash
docker exec fc-eval python3 /eval/evolve.py run --taskset polyglot-py --iterations 1 --k 3 --budget-usd 8
```

在评测器容器里跑,用它的 CP、模型代理与默认层;`status` 子命令打印档案与迭代。先 `POST /eval/tasksets/polyglot-py/check` 过导入检查。

## 真实走一遍(`ops/`)

单元测试里的报告是假的,所以一件事要算做完,得在部署上用真实模型真实走一遍(D18 记录了只有这样才露出来的五个问题)。`ops/` 下的脚本都在开发机上跑,用密钥目录里的令牌(只在机上读,不打印),只对运行中的 Agent 服务说话,**走完自己收拾**:开的 PR 关掉、批准草稿否决、它们写进记忆的行删掉、临时建的 agent 删掉。

本仓库保留通用的走查脚本:`ops/journey.py`(客户整条旅程)、`ops/demo-account.py`、`ops/bench.py`、`ops/home-tasks.py`、`ops/dogfood-*.py`(自己的开发回路在沙箱里)。针对项目自己部署的几份一次性走查(纠正、审查、提问、规则门)留在私有仓库,没有开源。

走脚本会真的花一点模型费用(每个几美分)。CI 在跑时不要走(CI 会重启节点),版本循环在跑时也不要走。

## 已知的风险:控制面无令牌

`CP_TOKEN` 不设置时控制面不校验调用方。它只绑定 127.0.0.1,但开发机是多人共用的:同机的其他登录用户能经它创建沙箱、在沙箱里执行命令、删除沙箱。统一加令牌要改 CI 的全部套件、工作台与评测器的调用,放在下一次动控制面时一起做;在那之前,这台机器上的其他用户被视为可信。

## Gitea 的 agent 账号

接单层以 `cairn-bot` 账号回帖、推分支、开 PR。账号与令牌由人创建(agent 不创建账号):在 Gitea 上注册用户 `cairn-bot`,给 `William2333ZZ/cairn` 写权限,生成一个 scope 为 `write:repository`、`write:issue` 的令牌,写进 `./secrets/gitea-cairn-bot.token`(0600)。没有这个文件时接单层仍能工作:仓库是公开的,匿名克隆;但不能回帖、不能开 PR,Ticket 在 ready 时直接结束,结果只在面板上。

## 尚未实现

评测器默认检查令牌(等直接调它的实验脚本都改带令牌);多用户的 Postgres;对象存储;多节点真机部署;桌面端安装。
