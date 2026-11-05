# 用 Claude Code 把沙箱当工具

目标:在 Claude Code 里对话,让它自己建一台沙箱、在里面跑命令、写文件,危险的一步前打检查点、出错就回滚,最后清理干净。用的是 MCP 服务器 `cairnsandbox.mcp`;工具与协议细节见 [MCP 参考](../mcp.md)。

## 前提

1. 一个可达的控制面(默认 `http://<主机>:18700`),部署见 [deploy/README.md](../../../deploy/README.md)。
2. 一枚令牌,以及你要用的项目。令牌从项目 owner 处拿([手册第 1 节](../manual.md));自己部署的话令牌是 `deploy/secrets/cp.token`。
3. 本机装好 Python 3.9+ 和 SDK(只用标准库):

   ```bash
   pip install -e sdk/python
   ```

4. 已安装 Claude Code(`claude` 命令可用)。

## 注册 MCP 服务器

```bash
claude mcp add aios \
  -e CAIRN_URL=http://127.0.0.1:18700 \
  -e CAIRN_TOKEN=<你的令牌> \
  -e CAIRN_PROJECT=acme \
  -- python3 -m cairnsandbox.mcp
```

三个环境变量:

| 变量 | 作用 | 不给时 |
| --- | --- | --- |
| `CAIRN_URL` | 控制面地址 | `http://127.0.0.1:18700` |
| `CAIRN_TOKEN` | Bearer 令牌 | 不发 `Authorization`,只有没设令牌的控制面才放行 |
| `CAIRN_PROJECT` | 沙箱归哪个项目 | 令牌所在账户的 default 项目 |

令牌也可以不写进注册命令:先在启动 Claude Code 的 shell 里 `export CAIRN_TOKEN=…`,再用 `claude mcp add aios -- python3 -m cairnsandbox.mcp`,子进程继承环境。令牌写进配置文件等于把它留在磁盘上,团队共享的配置里不要这样做。

确认已注册:`claude mcp list` 应当看到 `aios`;在 Claude Code 里输入 `/mcp` 能看到它提供的 12 个工具:`sandbox_create`、`sandbox_run`、`sandbox_write_file`、`sandbox_read_file`、`sandbox_checkpoint`、`sandbox_rollback`、`sandbox_fork`、`sandbox_pause`、`sandbox_resume`、`sandbox_kill`、`sandbox_list`、`sandbox_info`。

## 第一次对话

在 Claude Code 里这样说(换成你自己的事):

> 用 aios 的沙箱建一台 512 MiB 的机器,允许访问 pypi.org。在里面写一个 `fib.py` 打印前 20 个斐波那契数,跑一下,把输出给我。

它会依次调用:

1. `sandbox_create`,参数大致是 `{"mem": 512, "network": {"allow_hosts": ["pypi.org"]}}`,返回 `{"id": "sb-…", "status": "running", "node": "…"}`。
2. `sandbox_write_file`,把 `fib.py` 写到 `/home/agent/fib.py`(沙箱里默认用户是 `agent`)。
3. `sandbox_run`,`python3 /home/agent/fib.py`,返回 `exit`、`stdout`、`stderr`。

这时到工作台的「沙箱」页能看到这台机器,点进去还能开右栏终端看它的现场([手册第 4 节](../manual.md))。

几个要知道的:

- 沙箱默认**没有外网**,只放 `allow_hosts` 里列的主机。需要 `pip install`,就在建的时候告诉它放行 `pypi.org` 与 `files.pythonhosted.org`。
- Claude Code 第一次调用 MCP 工具时会向你要权限,按你的习惯允许即可。
- 工具失败时(例如 429 没有容量、402 当天预算用完),MCP 把错误以 `状态码: 信息` 的文字回给模型,模型通常会自己解释并调整。

## 检查点与回滚

这是 agent 做大改动时最有用的一招:先打点,试错,坏了就回去。对话里可以直接要求:

> 在动 `/home/agent/app` 之前先打一个叫 `before-refactor` 的检查点。然后把里面所有 `.py` 文件的 `print` 改成 `logging`,跑测试。如果测试挂了就回滚到这个检查点,换个办法。

对应的工具调用:

1. `sandbox_checkpoint`:`{"sandbox": "sb-…", "name": "before-refactor", "note": "clean tree"}`。运行中打,有几百毫秒的停顿。名字只能用小写字母、数字、`_ . -`,最长 40 个字符,重名会 409。
2. 若干 `sandbox_run` 做修改和测试。
3. 失败时 `sandbox_rollback`:`{"sandbox": "sb-…", "checkpoint": "before-refactor"}`。原地回到那一刻,id、目录、网络都不变,文件系统和内存里的进程都回去了。

想**并排试两种办法**而不是来回回滚,用 `sandbox_fork`:`{"sandbox": "sb-…", "n": 2, "checkpoint": "before-refactor"}`,得到两台从同一点出发的克隆(默认类别是 `ephemeral`),各试一条路,留下好的、删掉另一台。父沙箱不动。

## 长期不用:暂停

`sandbox_pause` 把沙箱落盘,不再计 CPU 与内存;`sandbox_resume` 恢复,或者直接对它 `sandbox_run`,会自动恢复。`persistent` 的沙箱空闲到期后也会自己暂停。

## 清理

对话结束前对 Claude 说"把你建的沙箱都删了"。它会 `sandbox_list` 找出沙箱,再逐个 `sandbox_kill`。注意 `sandbox_list` 列的是**这枚令牌能看到的全部沙箱**(同账户的都在),不只是它这次建的,让它按 id 删,别按"全部删"。

没删干净也不会一直烧钱:`ephemeral` 空闲到期会被销毁,`persistent` 会被暂停;暂停的沙箱只计磁盘。可以在工作台「控制台 → 概览」或「沙箱」页检查,也可以在「账单」看今天花了多少。

撤掉注册:`claude mcp remove aios`。

## 出了问题

| 现象 | 原因与办法 |
| --- | --- |
| `/mcp` 里 `aios` 是失败状态 | 多半是 `python3` 找不到 `cairnsandbox`:确认 `pip install -e sdk/python` 装在 Claude Code 用的那个 `python3` 里 |
| 工具返回 `401` | 令牌错、已被换掉或过期;让 owner 重发 |
| `403` | `CAIRN_PROJECT` 不是你账户的项目 |
| `429` / `402` | 没有容量或碰了配额 / 当天预算,见[手册第 14 节](../manual.md) |
| `sandbox_run` 里联网失败 | 创建时没放行那个域名;建一台新的带上 `network`,或让 owner 放宽(成员改策略要审批) |
| 想用的功能 MCP 没有 | MCP 只有这 12 个工具;预算、工单、卷、构建、账单用 [Python SDK](../sdk.md) |
