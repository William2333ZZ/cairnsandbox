# Use a Sandbox as a Tool with Claude Code

Goal: chat in Claude Code and let it build a sandbox by itself, run commands and write files in it, take a checkpoint before a risky step, roll back on errors, and clean up at the end. This uses the MCP server `cairnsandbox.mcp`; for tool and protocol details see the [MCP reference](../mcp.md).

## Prerequisites

1. A reachable control plane (default `http://<host>:18700`); for deployment see [deploy/README.md](../../../../deploy/README.md).
2. A token, and the project you want to use. Get the token from the project owner ([manual section 1](../manual.md)); if you deploy it yourself, the token is `deploy/secrets/cp.token`.
3. Python 3.9+ and the SDK installed locally (it uses only the standard library):

   ```bash
   pip install -e sdk/python
   ```

4. Claude Code installed (the `claude` command is available).

## Register the MCP server

```bash
claude mcp add aios \
  -e CAIRN_URL=http://127.0.0.1:18700 \
  -e CAIRN_TOKEN=<your token> \
  -e CAIRN_PROJECT=acme \
  -- python3 -m cairnsandbox.mcp
```

Three environment variables:

| Variable | Purpose | If not given |
| --- | --- | --- |
| `CAIRN_URL` | Control plane address | `http://127.0.0.1:18700` |
| `CAIRN_TOKEN` | Bearer token | No `Authorization` is sent; only a control plane with no token configured lets this through |
| `CAIRN_PROJECT` | Which project the sandboxes belong to | The default project of the token's account |

You can also keep the token out of the registration command: first `export CAIRN_TOKEN=…` in the shell that launches Claude Code, then use `claude mcp add aios -- python3 -m cairnsandbox.mcp`; the child process inherits the environment. Writing the token into a config file leaves it on disk, so don't do that in a config shared by a team.

To confirm registration: `claude mcp list` should show `aios`; typing `/mcp` in Claude Code shows the 12 tools it provides: `sandbox_create`, `sandbox_run`, `sandbox_write_file`, `sandbox_read_file`, `sandbox_checkpoint`, `sandbox_rollback`, `sandbox_fork`, `sandbox_pause`, `sandbox_resume`, `sandbox_kill`, `sandbox_list`, `sandbox_info`.

## The first conversation

Say this in Claude Code (substitute your own task):

> Use aios's sandbox to create a 512 MiB machine that is allowed to reach pypi.org. In it, write a `fib.py` that prints the first 20 Fibonacci numbers, run it, and show me the output.

It will call, in order:

1. `sandbox_create`, with parameters roughly `{"mem": 512, "network": {"allow_hosts": ["pypi.org"]}}`, returning `{"id": "sb-…", "status": "running", "node": "…"}`.
2. `sandbox_write_file`, writing `fib.py` to `/home/agent/fib.py` (the default user in the sandbox is `agent`).
3. `sandbox_run`, `python3 /home/agent/fib.py`, returning `exit`, `stdout`, `stderr`.

At this point you can see the machine on the workbench's "Sandboxes" page, and clicking into it lets you open the terminal in the right pane to watch it live ([manual section 4](../manual.md)).

A few things to know:

- Sandboxes have **no outside network** by default; only the hosts listed in `allow_hosts` are allowed. If you need `pip install`, tell it at creation time to allow `pypi.org` and `files.pythonhosted.org`.
- The first time Claude Code calls an MCP tool it asks you for permission; allow it as you normally would.
- When a tool fails (for example 429 no capacity, 402 today's budget used up), MCP returns the error to the model as text of the form `status code: message`, and the model will usually explain it and adjust by itself.

## Checkpoints and rollback

This is the most useful trick when an agent makes big changes: take a checkpoint first, experiment, and go back if it breaks. You can ask for it directly in the conversation:

> Before touching `/home/agent/app`, take a checkpoint named `before-refactor`. Then change `print` to `logging` in all `.py` files in it and run the tests. If the tests fail, roll back to this checkpoint and try a different approach.

The corresponding tool calls:

1. `sandbox_checkpoint`: `{"sandbox": "sb-…", "name": "before-refactor", "note": "clean tree"}`. Taking one while running causes a pause of a few hundred milliseconds. Names may only use lowercase letters, digits, and `_ . -`, at most 40 characters; a duplicate name gives 409.
2. Several `sandbox_run` calls to make changes and run tests.
3. On failure, `sandbox_rollback`: `{"sandbox": "sb-…", "checkpoint": "before-refactor"}`. It returns in place to that moment; the id, directories, and network are unchanged, and both the filesystem and the processes in memory go back.

To **try two approaches side by side** instead of rolling back and forth, use `sandbox_fork`: `{"sandbox": "sb-…", "n": 2, "checkpoint": "before-refactor"}`, which gives two clones starting from the same point (the default class is `ephemeral`); each tries one path, and you keep the good one and delete the other. The parent sandbox is untouched.

## Long periods of disuse: pause

`sandbox_pause` writes the sandbox to disk and stops charging CPU and memory; `sandbox_resume` resumes it, or you can just `sandbox_run` on it, which resumes it automatically. A `persistent` sandbox also pauses itself after it has been idle until expiry.

## Cleanup

Before the conversation ends, tell Claude "delete all the sandboxes you created". It will find the sandboxes with `sandbox_list` and then `sandbox_kill` them one by one. Note that `sandbox_list` lists **all sandboxes this token can see** (everything in the same account), not just the ones it created this time, so have it delete by id rather than "delete all".

If not everything is cleaned up it won't keep burning money forever: an `ephemeral` sandbox is destroyed when idle expiry hits, and a `persistent` one is paused; a paused sandbox is charged for disk only. You can check on the workbench under "Console → Overview" or the "Sandboxes" page, and see how much you spent today under "Billing".

To remove the registration: `claude mcp remove aios`.

## When something goes wrong

| Symptom | Cause and fix |
| --- | --- |
| `aios` is in a failed state in `/mcp` | Most likely `python3` can't find `cairnsandbox`: make sure `pip install -e sdk/python` was installed into the very `python3` that Claude Code uses |
| A tool returns `401` | The token is wrong, has been replaced, or has expired; ask the owner to reissue it |
| `403` | `CAIRN_PROJECT` is not a project of your account |
| `429` / `402` | No capacity, or a quota / today's budget was hit; see [manual section 14](../manual.md) |
| Networking fails inside `sandbox_run` | That domain was not allowed at creation; create a new one with `network`, or ask the owner to relax it (members changing the policy need approval) |
| A feature you want isn't in MCP | MCP has only these 12 tools; for budgets, tickets, volumes, builds, and billing use the [Python SDK](../sdk.md) |
