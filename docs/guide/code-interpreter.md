# code-interpreter:`e2b_code_interpreter` 的 `run_code`

官方 `e2b_code_interpreter` SDK 的 `Sandbox.create()` + `run_code(...)` 可以直接对着 CairnSandbox 用:`E2B_API_URL` 与 `E2B_SANDBOX_URL` 指到控制面的 E2B 口(见 [api/e2b.md](api/e2b.md)),SDK 默认的模板叫 `code-interpreter-v1`,下面是怎么在你的部署里建出这个模板。

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
| 富输出 | 文本、对象的 `_repr_html_` / `_repr_markdown_` / `_repr_svg_` / `_repr_png_` / `_repr_json_`(DataFrame 带 html),**matplotlib 的图**作为 PNG 结果返回(用完关闭) | 完整的 Jupyter 富输出 |
| 图表的结构化 `chart` 对象、IPython 魔法命令、widgets | **没有**(图只有 PNG) | 有 |
| 预装的库 | 用 `ci-pylibs` 层:numpy、pandas、matplotlib、seaborn、scipy、scikit-learn、pillow、sympy、requests、httpx、beautifulsoup4、lxml、openpyxl、pyyaml、tqdm(Python 3.13);不建这一层就只有标准库和 jinja2 | numpy、pandas、matplotlib 等一大批 |
| 状态 | Python 上下文是常驻子进程,变量在 `run_code` 之间保持;bash 与 javascript **每次执行是独立进程,没有状态** | 内核状态 |

**没有的包要自己加**:沙箱默认出口关闭,`pip install` 联不了网。往 `recipes/ci-pylibs/requirements.txt` 加包名,在有网的机器上跑 `recipes/ci-pylibs/fetch-wheels.sh` 取 wheel(只收 Linux x86_64、CPython 3.13 的二进制 wheel;只有源码包的装不了),再重建层与模板。这些包是第三方的,各有自己的许可证(numpy、pandas、scipy、scikit-learn 等是 BSD 类,matplotlib 是自己的宽松许可证,pillow 是 HPND),你分发带它们的模板时要看一下。

## 建模板

需要 agent-langs 层(CPython 3.13 加 bun)。它的文件(`cpython-*.tar.gz`、`bun`、`wheels/`)在有外网的机器上下载,放进 `recipes/agent-langs/files/`,见 `recipes/agent-langs/build.sh`。

```bash
# 1. 层(在节点所在的机器上;层与模板都住在构建它们的那台节点上)
node/build-layer.sh recipes/agent-langs debian-13 "" http://127.0.0.1:18800        # 打印层 id,例如 L-5f38d7a4
#    要 numpy / pandas / matplotlib 等:在有网的机器上取 wheel,再把 ci-pylibs 层叠在 agent-langs 之上
recipes/ci-pylibs/fetch-wheels.sh                                                      # 约 110 MB,放在 recipes/ci-pylibs/files/(已 gitignore)
BUILD_MEM=3072 node/build-layer.sh recipes/ci-pylibs debian-13 L-5f38d7a4 http://127.0.0.1:18800   # 第三个参数是父层;例如 L-6148aa3d
# 2. 模板:起一台带这些层的沙箱,放进 server.py,启动,验证一次 cell,再把"正在运行的沙箱"提交成模板(已有同名模板会被替换)
python3 recipes/code-interpreter/build-template.py --cp http://127.0.0.1:18700 --token "$(cat deploy/secrets/cp.token)" \
    --layer L-5f38d7a4 --layer L-6148aa3d --node fc-node --mem 2048
```

模板是**内存加磁盘**的快照,所以服务和它预热好的 Python 内核(numpy、pandas、matplotlib 已经 import 过)已经在 49999 上监听,新沙箱一出来就能用。带这些包的模板快照的内存约 243 MiB,虚机内存 2048 MiB;层的打包约 1.4 GiB,构建时 `BUILD_MEM` 要给够(3072)。多节点时模板只在构建它的节点上,调度会优先选有这个模板的节点;没有现成的"复制到所有节点"命令(`POST /templates/everywhere` 是按规格重新构建,不是复制)。

## SDK 怎么连到沙箱里的服务

SDK 对 49999 的请求带 `E2b-Sandbox-Id` 和 `E2b-Sandbox-Port: 49999` 头,发到 `E2B_SANDBOX_URL`(不需要泛域名)。控制面看到端口不是 envd 的 49983,就按端口转发给节点的端口代理(流式)。凭据:`/execute` 带 `X-Access-Token`,而 `/contexts` 这类调用**只带 `E2B-Traffic-Access-Token`**,所以创建响应里除了 `envdAccessToken` 还有一个**绑定到这台沙箱的流量令牌 `trafficAccessToken`**;两个令牌任何一个都能打开这台沙箱的端口,别的沙箱的令牌不行,没有令牌 401。审计里有 `op: envd_port`(端口、方法、路径),`/metrics` 里算进 `cairn_envd_*`。

## 验证

`ops/e2b-code-interpreter-conformance.py`(17 条用例,含 numpy / pandas、matplotlib 图返回 PNG、scipy / scikit-learn 等能 import 和运行)对着 2026-11-05 的一个 2 节点演练栈:`e2b-code-interpreter` 2.10.5、2.11.0、2.11.9(配 e2b 2.63.0)**全部通过**。经 SSH 隧道量到(含网络往返):创建 0.17–0.23 s,第一次 import pandas + numpy + matplotlib 0.07–0.19 s(已预加载),10 万行 DataFrame 的 `describe` 0.15 s,一张 matplotlib 图回传 PNG 0.21–0.28 s。结果在 [docs/research/e2b-conformance/](../research/e2b-conformance/)。

**没验证**:
- 2.0–2.4 这些老版本:它们和当前的 e2b 2.63.0 配起来就崩(访问已被移除的私有属性 `_transport`),这是 SDK 自己的问题,我们没有配套的旧 e2b 版本去测;1.x 不认自定义 API 地址,套件拒绝向它发请求。
- 结构化的 `chart` 对象、R、Java、IPython 魔法命令(见上表,不支持);没有的第三方包(要自己加);大数据量或长时间运行的计算。
- 长时间运行或大量并发的 `run_code`;多节点下的模板分发。
- JS 版 `@e2b/code-interpreter`。
