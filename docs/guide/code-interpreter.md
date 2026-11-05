# code-interpreter:`e2b_code_interpreter` 的 `run_code`

官方 `e2b_code_interpreter` SDK 的 `Sandbox.create()` + `run_code(...)` 可以直接对着 Cairn 用:`E2B_API_URL` 与 `E2B_SANDBOX_URL` 指到控制面的 E2B 口(见 [api/e2b.md](api/e2b.md)),SDK 默认的模板叫 `code-interpreter-v1`,下面是怎么在你的部署里建出这个模板。

```python
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create()                       # 从模板快照热启动,实测 0.2 s
print(sbx.run_code("x = 40\nx + 2").text)    # 42,状态留在沙箱里的 Python 内核
```

## 它是什么,不是什么

E2B 官方的 code-interpreter 模板里跑的是 Jupyter 内核加一个 FastAPI 服务(端口 49999)。我们的是**自己写的一个小服务**(`recipes/code-interpreter/server.py`,只用 Python 标准库),说同一套 HTTP 协议,所以 SDK 不用改。

| | 我们的服务 | E2B 官方模板 |
|---|---|---|
| `run_code`:最后一个表达式的结果、stdout / stderr 分开、报错带 name / value / traceback、`on_stdout` / `on_result` / `on_error` 回调、流式 | 有 | 有 |
| 上下文:创建、列出、隔离的状态、`cwd`、重启、删除 | 有 | 有 |
| 语言 | python、bash、javascript(有 bun 或 node 时) | python、javascript、r、java、bash 等 |
| 富输出 | 文本,以及对象的 `_repr_html_` / `_repr_markdown_` / `_repr_svg_` / `_repr_png_` / `_repr_json_` | 完整的 Jupyter 富输出 |
| 图表(matplotlib 的 `chart`)、IPython 魔法命令、widgets | **没有** | 有 |
| 预装的库 | **只有标准库和 jinja2**(Python 3.13,来自 agent-langs 层) | numpy、pandas、matplotlib 等一大批 |
| 状态 | Python 上下文是常驻子进程,变量在 `run_code` 之间保持;bash 与 javascript **每次执行是独立进程,没有状态** | 内核状态 |

要 numpy / pandas / matplotlib:沙箱默认出口关闭,装包要在构建模板时做(给 agent-langs 之外再叠一层带这些包的层,或构建时临时放开出口)。这一步我们没做,也没验证过。

## 建模板

需要 agent-langs 层(CPython 3.13 加 bun)。它的文件(`cpython-*.tar.gz`、`bun`、`wheels/`)在有外网的机器上下载,放进 `recipes/agent-langs/files/`,见 `recipes/agent-langs/build.sh`。

```bash
# 1. 层(在节点所在的机器上;层与模板都住在构建它们的那台节点上)
node/build-layer.sh recipes/agent-langs debian-13 "" http://127.0.0.1:18800        # 打印层 id,例如 L-5f38d7a4
# 2. 模板:起一台带这个层的沙箱,放进 server.py,启动,验证一次 cell,再把"正在运行的沙箱"提交成模板
python3 recipes/code-interpreter/build-template.py --cp http://127.0.0.1:18700 --token "$(cat deploy/secrets/cp.token)" \
    --layer L-5f38d7a4 --node fc-node
```

模板是**内存加磁盘**的快照,所以服务和它预热好的 Python 内核已经在 49999 上监听,新沙箱一出来就能用。多节点时模板只在构建它的节点上,调度会优先选有这个模板的节点;没有现成的"复制到所有节点"命令(`POST /templates/everywhere` 是按规格重新构建,不是复制)。

## SDK 怎么连到沙箱里的服务

SDK 对 49999 的请求带 `E2b-Sandbox-Id` 和 `E2b-Sandbox-Port: 49999` 头,发到 `E2B_SANDBOX_URL`(不需要泛域名)。控制面看到端口不是 envd 的 49983,就按端口转发给节点的端口代理(流式)。凭据:`/execute` 带 `X-Access-Token`,而 `/contexts` 这类调用**只带 `E2B-Traffic-Access-Token`**,所以创建响应里除了 `envdAccessToken` 还有一个**绑定到这台沙箱的流量令牌 `trafficAccessToken`**;两个令牌任何一个都能打开这台沙箱的端口,别的沙箱的令牌不行,没有令牌 401。审计里有 `op: envd_port`(端口、方法、路径),`/metrics` 里算进 `cairn_envd_*`。

## 验证

`ops/e2b-code-interpreter-conformance.py`(14 条用例)对着 2026-11-05 的一个 2 节点演练栈:`e2b-code-interpreter` 2.10.5、2.11.0、2.11.9(配 e2b 2.63.0)**全部通过**。结果在 [docs/research/e2b-conformance/](../research/e2b-conformance/)。

**没验证**:
- 2.0–2.4 这些老版本:它们和当前的 e2b 2.63.0 配起来就崩(访问已被移除的私有属性 `_transport`),这是 SDK 自己的问题,我们没有配套的旧 e2b 版本去测;1.x 不认自定义 API 地址,套件拒绝向它发请求。
- 图表、numpy / pandas、R、Java、IPython 魔法命令(见上表,不支持)。
- 长时间运行或大量并发的 `run_code`;多节点下的模板分发。
- JS 版 `@e2b/code-interpreter`。
