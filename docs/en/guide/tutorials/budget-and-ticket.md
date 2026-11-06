# Budgets, Tickets, and Reports

This is the layer that is unique to us: the sandbox **knows who it is working for, why, and how much it has spent**. From the outside you set a budget, write a ticket, and send inputs; the harness inside the sandbox reads them through the kernel ABI. When it finishes or gets stuck it sends a report, and you read events and wait for the report from the outside. E2B and ordinary sandbox services do not have this layer.

The full ABI definition is in [CONTRACT §7](../../../../CONTRACT.md); SDK methods are in [sdk.md](../sdk.md); HTTP is in [api/state.md](../api/state.md).

## Four actions

| What you do | SDK | HTTP | What the harness sees in the sandbox |
| --- | --- | --- | --- |
| Set a budget | `sb.budget(max_usd=, max_tokens=)` | `PUT /sandboxes/<id>/budget {max_tokens?, max_usd?}` | ABI `budget_get` reads the budget; every model call records its cost with `budget_charge {tokens_in, tokens_out, cost_usd}` and gets back `over`; when exceeded, the harness ends as failed |
| Write a ticket | `sb.ticket({...})` | `PUT /sandboxes/<id>/ticket {ticket:{id, short, text, scope, branch, base_branch, base_commit, cwd, rules, inputs, thread}}` | The file `/run/aios/ticket.json`; ABI `ticket_get` → `{ticket, inbox_n}`. In ticket mode the task text comes from it |
| Send input | `sb.inbox(text, kind=, actor=, thread=)` | `POST /sandboxes/<id>/inbox {kind, text, actor?, thread?}` → 201 `{n}` | Appended to `/run/aios/inbox.jsonl`; ABI `inbox {since}` → `{inputs:[{n, ts, kind, text, actor?}], last_n}`, which the harness reads once per step |
| Read events / wait for the report | `sb.events(since=)`, `sb.wait_for_report()` | `GET /sandboxes/<id>/events?since=&max=` → `{events, last_seq}` | The harness sends `event {type, data}`, `checkpoint {note}`, and `report {state, text, attention, deliverable}` through the ABI; all of them are written to the event stream |

Key points:

- **The budget is kernel state of the sandbox**, not something the harness does out of good manners. Model calls additionally go through the model proxy, which also has a budget per sandbox token and returns 402 when it is exceeded ([CONTRACT §4](../../../../CONTRACT.md)). When the harness hits an exhausted budget (`BudgetExceeded`), it ends with a `failed` report.
- The event log `/run/aios/events.jsonl` is **not readable** by the agent inside the sandbox; the host fetches it through sandboxd, so the agent cannot tamper with its own record.
- An event is `{seq, ts, uid, type, data}`; `seq` increases, and you pull incrementally with `since`.
- A report's `state` is one of `progress | waiting | ready | blocked | failed | dropped`. `ready`: done, and may carry `deliverable {branch, head, base}`; `waiting`: stuck waiting for a person, with `attention.question` being the question; `blocked` / `failed`: blocked or failed.

## A 15-line example

Prerequisites: the sandbox has a harness (`/aios/bin/aios`, from the two layers "agent-harness" and "agent-langs"; the layer ids are put by the deployer in `EVAL_LAYERS` / `INTAKE_LAYERS` in `deploy/.env`), there is a git repository in `/work/repo`, you have the model proxy's address and a sandbox token (`CAIRN_MODEL_URL` / `CAIRN_MODEL_KEY`; the token is obtained from the model proxy's `POST /tokens`), and the sandbox allows the model proxy's host. Below, `LAYERS` and `MODEL_ENV` stand in for these.

```python
from cairnsandbox import Client
c = Client()                                                       # CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
sb = c.sandboxes.create(layers=LAYERS, mem=1024, network={"allow_hosts": [MODEL_HOST]})
sb.budget(max_usd=0.50)                                            # spend at most 0.5 USD on this job
sb.ticket({"id": "t-1", "short": "t1", "text": "Change the install section of the README to pip install -e .",
           "cwd": "/work/repo", "branch": "ticket/t1", "base_branch": "main"})
sb.commands.run("setsid nohup /aios/bin/aios run --ticket --out /tmp/traj.json --cwd /work/repo "
                ">/tmp/aios.log 2>&1 &", env=MODEL_ENV)            # start the harness in the background, returns immediately
sb.inbox("Don't touch the docs/ directory", kind="note", actor="me")               # add a line mid-run; the harness reads it on its next step
rep = sb.wait_for_report(timeout=900)                              # returns only on ready / waiting / blocked / failed; progress does not count
print(rep and rep["data"])                                         # None = timed out
print(sb.diff(cwd="/work/repo", base="main")["stat"])              # see what it changed
sb.kill()
```

How to read it:

1. Creating the sandbox, setting the budget, and writing the ticket are done **before** the harness starts, so the harness can read them as soon as it comes up.
2. The harness runs in `--ticket` mode: the task text comes from `ticket_get`; every step reads the inbox; at the end it sends **one** terminal report. If the submitted text starts with `QUESTION:` it becomes `waiting` with `attention`; `BudgetExceeded` becomes `failed`; when there are new commits on the branch, the `deliverable` of `ready` carries the branch and head.
3. `sb.diff()` runs `git diff main...HEAD` as root inside the sandbox and returns `stat` and `diff`.
4. To answer its question: `sb.inbox("answer…", kind="answer", actor="me")`, start the harness again (the ticket and the previous workspace are still there), and `wait_for_report` again.

By default `wait_for_report` returns only when a state that ends the round (ready / waiting / blocked / failed) appears. To see `progress` as well, pass `include_progress=True`, or use `sb.events(since=seq)` to get `(events, last_seq)`, pull incrementally yourself, and pick the ones with `type == "report"`.

You can see the same in the workbench: the "Event stream" panel at the top right of the sandbox detail page (refreshed every two seconds) shows these events, and the "diff" panel shows the branch against its base.

## You can try it without a harness

If you only want to see how the ABI works, you do not need to run a model. After setting a budget and a ticket on an ordinary sandbox, read the socket as the `agent` user:

```python
sb.budget(max_tokens=100000)
sb.ticket({"id": "t-0", "short": "t0", "text": "hello"})
sb.inbox("first input")
```

Then, inside the sandbox, connect to `/run/aios/abi.sock` with Python (NDJSON, one request per line) and send `{"id":1,"op":"hello"}`, `{"id":2,"op":"ticket_get"}`, `{"id":3,"op":"inbox","since":0}`, `{"id":4,"op":"report","state":"ready","text":"ok"}`. Back outside, `sb.events()` shows that `report`. The ABI socket identifies callers by the connecting uid (`SO_PEERCRED`).

## Cleanup and cost

Usage is accounted per sandbox: `sb.usage()` (read it before `kill()`), `c.billing.today()`, and the Billing page of the console shows the project total; after a sandbox is `kill()`ed, CPU and memory are no longer charged. Model cost is set by the model proxy and recorded under this sandbox.
