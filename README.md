# Cairn

**给 agent 一个可以放心试错的地方:出了岔子,回到上一个标记点;越界的事,要有人点头;做过什么,有记录可查。**

> English: [README.en.md](README.en.md) · [docs/en/](docs/en/README.md)

agent 写代码、跑命令、装依赖,总会出错,也总有一天会做不该做的事。Cairn 把它放进一台随时可以倒回去的虚拟机里:

* **倒得回去。** 运行中打检查点(约几十毫秒),原地回滚(id 与网络不变),或从任一检查点 fork 出 n 份各试各的。
* **出不去,除非你批。** 沙箱没有直连网络;出站请求经代理,只放行白名单。成员想放宽,要所有者批准;密钥可以在代理处注入,不进沙箱。
* **查得到。** 控制面做过的每件事(包括每次 `exec` 与文件读写)记进哈希链,可以核对没被改过。
* **知道在为谁干活。** 账户、项目、成员、配额、预付额度与用量账单,都在沙箱这一层。

底下是 Firecracker microVM(由 jailer 启动),上面是一个控制面、Python 与 JS SDK、CLI、MCP 和控制台;另有一个可选的 **agent 层**:一个托管的个人编码 agent,
沙箱认识它的预算、工单和事件,它在沙箱里做事,做完、卡住或要你做决定时才找你(详见下面"agent 层")。两层的接缝只有一道:agent 层向沙箱云要一台沙箱,通过 HTTP 或 SDK;不用 agent 层,沙箱云照样跑。

## 五分钟

```bash
scripts/bootstrap.sh && cd deploy && docker compose up -d
pip install -e sdk/python
CAIRN_URL=http://127.0.0.1:18700 CAIRN_TOKEN=$(cat deploy/secrets/cp.token) python3 sdk/python/examples/quickstart.py
```

```python
from cairnbox import Client
c = Client("http://127.0.0.1:18700", token="…", project="acme")
with c.sandboxes.create(mem=512, network={"allow_hosts": ["pypi.org"]}) as sb:
    print(sb.commands.run("python3 -V").check().stdout)
    sb.checkpoints.create("clean")
    sb.commands.run("rm -rf /home/agent/*")
    sb.checkpoints.rollback("clean")                  # 文件回来了
    clone = sb.fork(checkpoint="clean")[0]            # 从同一点分出一份独立的
```

要求 Linux x86_64、`/dev/kvm`、Docker 24+(compose v2)、约 15 GB 磁盘;第一次装要编译 guest 内核,30–60 分钟。
JS / TS:`sdk/js`(`cairnbox`,零依赖)。给 agent 当工具:`claude mcp add cairn -- python3 -m cairnbox.mcp`。另有一个 E2B 形状的 REST 口(18710 端口),常见的 E2B 客户端代码改个地址就能用。

| | |
| --- | --- |
| ![沙箱详情](docs/screenshots/08-sandbox-detail.png) | ![审计](docs/screenshots/06-console-audit.png) |
| 一台沙箱:规格、网络与限额、检查点打 / 回滚 / fork | 审计:控制面做过的每件事,哈希链可核对 |

完整功能清单:[docs/guide/features.md](docs/guide/features.md)。用的人看 [docs/guide/](docs/guide/README.md)(租户手册、API 参考、SDK 与 MCP 参考、教程)。

## agent 层

一个托管的、会变强的个人编码 agent:接进你已经在用的地方(代码托管、聊天工具),在对话里交代一件事,然后走开。它只在**做完了、卡住了、要你做决定**时找你;
不可逆的事(合并、向外发消息、超预算、定例行)只给你一份草稿,你同意才发生。你纠正它一次,它记成规则,以后每件事开始时带着,有检查的规则在交付前先被检查一遍。
一个人的一天见 [docs/001](docs/001-journey.md);工作台的演示不需要部署,见 [docs/008](docs/008-workbench.md)。这一层同样是 Apache-2.0,但比沙箱云更早期。

## 状态

单台机器上实测;**没有外部渗透测试**,没有对外基线,多节点未做压力。`scripts/bootstrap.sh` 本身没有从干净机器整条跑过(分步验证见 [docs/en/quickstart.md](docs/en/quickstart.md) 末尾),
项目之外还没人照着装过。完整的没做清单:[ROADMAP.md](ROADMAP.md)。安全问题:[SECURITY.md](SECURITY.md)。参与:[CONTRIBUTING.md](CONTRIBUTING.md)(提交要 `git commit -s`)与[行为准则](CODE_OF_CONDUCT.md)。

## 目录与文档

`cp/` 控制面(Go)· `node/` 节点代理(Python)· `guest/` guest 内的 runtime(Rust)· `sdk/` SDK、CLI、MCP · `console/` 控制台 · `deploy/` compose 与运维 · `recipes/` 内核、基础镜像、层的配方 ·
`aios/` `intake/` `eval/` `web/` agent 层。设计文档从 [docs/README.md](docs/README.md) 读起(000 原则 … 013 威胁模型),每个决定与理由在 [docs/decisions-index.md](docs/decisions-index.md)(中文);各部件的索引在 [docs/011](docs/011-architecture.md)。

许可证:Apache-2.0([LICENSE](LICENSE)),第三方内容见 [NOTICE](NOTICE)。
