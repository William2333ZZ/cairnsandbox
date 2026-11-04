# 预算、工单与报告

这是我们独有的那一层:沙箱**知道它在给谁、为什么干活、花了多少**。你从外面给它设预算、写工单、送输入,沙箱里的 harness 经内核 ABI 读到这些;它干完或卡住时发一份报告,你从外面读事件、等报告。E2B 与一般的沙箱服务没有这一层。

ABI 的全部定义在 [CONTRACT §7](../../../CONTRACT.md);SDK 的方法在 [sdk.md](../sdk.md);HTTP 在 [api/state.md](../api/state.md)。

## 四个动作

| 你做的 | SDK | HTTP | harness 在沙箱里看到 |
| --- | --- | --- | --- |
| 设预算 | `sb.budget(max_usd=, max_tokens=)` | `PUT /sandboxes/<id>/budget {max_tokens?, max_usd?}` | ABI `budget_get` 读到预算;每次模型调用 `budget_charge {tokens_in, tokens_out, cost_usd}` 记账并返回 `over`;超了 harness 以失败结束 |
| 写工单 | `sb.ticket({...})` | `PUT /sandboxes/<id>/ticket {ticket:{id, short, text, scope, branch, base_branch, base_commit, cwd, rules, inputs, thread}}` | 文件 `/run/aios/ticket.json`;ABI `ticket_get` → `{ticket, inbox_n}`。工单模式下任务文本就来自它 |
| 送输入 | `sb.inbox(text, kind=, actor=, thread=)` | `POST /sandboxes/<id>/inbox {kind, text, actor?, thread?}` → 201 `{n}` | 追加到 `/run/aios/inbox.jsonl`;ABI `inbox {since}` → `{inputs:[{n, ts, kind, text, actor?}], last_n}`,harness 每一步读一次 |
| 读事件 / 等报告 | `sb.events(since=)`、`sb.wait_for_report()` | `GET /sandboxes/<id>/events?since=&max=` → `{events, last_seq}` | harness 经 ABI 发 `event {type, data}`、`checkpoint {note}`、`report {state, text, attention, deliverable}`,都写进事件流 |

要点:

- **预算是沙箱的内核状态**,不是 harness 的自觉。模型调用另外经模型代理,代理按沙箱令牌也有一个预算,超了回 402([CONTRACT §4](../../../CONTRACT.md))。harness 遇到预算用尽(`BudgetExceeded`)以 `failed` 报告结束。
- 事件日志 `/run/aios/events.jsonl` 对沙箱里的 agent **不可读**,宿主经 sandboxd 取,所以 agent 篡改不了自己的记录。
- 事件是 `{seq, ts, uid, type, data}`;`seq` 递增,用 `since` 增量拉。
- 报告的 `state` 是 `progress | waiting | ready | blocked | failed | dropped` 之一。`ready`:做完了,可带 `deliverable {branch, head, base}`;`waiting`:卡住在等人,`attention.question` 是问题;`blocked` / `failed`:受阻或失败。

## 一个 15 行的例子

前提:沙箱里有 harness(`/aios/bin/aios`,来自"agent-harness"与"agent-langs"两个层,层 id 由部署者放在 `deploy/.env` 的 `EVAL_LAYERS` / `INTAKE_LAYERS`),`/work/repo` 里有一个 git 仓库,并给了模型代理的地址与沙箱令牌(`CAIRN_MODEL_URL` / `CAIRN_MODEL_KEY`,令牌从模型代理 `POST /tokens` 领取),沙箱放行了模型代理的主机。下面用 `LAYERS`、`MODEL_ENV` 代指这些。

```python
from cairnbox import Client
c = Client()                                                       # CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
sb = c.sandboxes.create(layers=LAYERS, mem=1024, network={"allow_hosts": [MODEL_HOST]})
sb.budget(max_usd=0.50)                                            # 这件事最多花 0.5 美元
sb.ticket({"id": "t-1", "short": "t1", "text": "把 README 的安装一节改成 pip install -e .",
           "cwd": "/work/repo", "branch": "ticket/t1", "base_branch": "main"})
sb.commands.run("setsid nohup /aios/bin/aios run --ticket --out /tmp/traj.json --cwd /work/repo "
                ">/tmp/aios.log 2>&1 &", env=MODEL_ENV)            # 后台起 harness,立刻返回
sb.inbox("别动 docs/ 目录", kind="note", actor="me")               # 干活途中再补一句,harness 下一步会读到
rep = sb.wait_for_report(timeout=900)                              # 只在 ready / waiting / blocked / failed 时返回;progress 不算
print(rep and rep["data"])                                         # None = 超时
print(sb.diff(cwd="/work/repo", base="main")["stat"])              # 看它改了什么
sb.kill()
```

读法:

1. 创建沙箱、设预算、写工单这三步在 harness 启动**之前**做,harness 一起来就能读到。
2. harness 用 `--ticket` 模式跑:任务文本来自 `ticket_get`;每一步读 inbox;结束发**一次**终态报告。提交文本以 `QUESTION:` 开头会变成 `waiting` 并带 `attention`;`BudgetExceeded` 变成 `failed`;分支上有新提交时 `ready` 的 `deliverable` 带分支与 head。
3. `sb.diff()` 在沙箱里以 root 跑 `git diff main...HEAD`,返回 `stat` 与 `diff`。
4. 回答它的提问:再 `sb.inbox("答案…", kind="answer", actor="me")`,再起一次 harness(工单与上一次的工作区仍在),重新 `wait_for_report`。

`wait_for_report` 默认只在结束这一轮的状态(ready / waiting / blocked / failed)出现时返回;要连 `progress` 也看,传 `include_progress=True`,或用 `sb.events(since=seq)` 拿 `(events, last_seq)` 自己增量拉、挑 `type == "report"` 的。

在工作台里同样能看:沙箱详情页右上角的"事件流"面板(每两秒刷新)就是这些事件;"diff"面板看分支对基线。

## 没有 harness 也能试

只想看 ABI 怎么通,不必跑模型。在一台普通沙箱里设了预算和工单后,以 `agent` 用户读 socket:

```python
sb.budget(max_tokens=100000)
sb.ticket({"id": "t-0", "short": "t0", "text": "hello"})
sb.inbox("第一条输入")
```

然后在沙箱里用 Python 连 `/run/aios/abi.sock`(NDJSON,每行一个请求),发 `{"id":1,"op":"hello"}`、`{"id":2,"op":"ticket_get"}`、`{"id":3,"op":"inbox","since":0}`、`{"id":4,"op":"report","state":"ready","text":"ok"}`,回到外面 `sb.events()` 就能看到那条 `report`。ABI 的 socket 按连接者的 uid 认人(`SO_PEERCRED`)。

## 清理与花费

用量按沙箱记账:`sb.usage()`(要在 `kill()` 之前读)、`c.billing.today()`,控制台「账单」里是项目的合计;沙箱 `kill()` 后不再计 CPU 与内存。模型费用由模型代理定、记在这台沙箱名下。
