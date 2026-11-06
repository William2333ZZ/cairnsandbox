# MCP Server

Source: [mcp.py](../../../sdk/python/cairnsandbox/mcp.py). It offers the sandbox cloud as tools to any agent that speaks MCP (Claude Code, Codex, Cursor, etc.). stdio, JSON-RPC 2.0, newline-delimited, zero dependencies. The underlying calls go through the [Python SDK](sdk.md); for routes see [api/sandboxes.md](api/sandboxes.md).

## Starting it

```
CAIRN_URL=http://cp:18700 CAIRN_TOKEN=… CAIRN_PROJECT=acme python3 -m cairnsandbox.mcp
```

`CAIRN_URL` (default `http://127.0.0.1:18700`), `CAIRN_TOKEN`, and `CAIRN_PROJECT` are read from the environment (`Client()` constructed with no arguments). You first need `pip install -e sdk/python`, or put `sdk/python` on `PYTHONPATH`.

### Connecting to Claude Code

```
claude mcp add aios -- python3 -m cairnsandbox.mcp
```

After `pip install -e sdk/python` there is also a console command `cairnsandbox-mcp`, equivalent to `python3 -m cairnsandbox.mcp`:

```bash
claude mcp add aios -- cairnsandbox-mcp
```

To pass environment variables: `claude mcp add aios -e CAIRN_URL=http://cp:18700 -e CAIRN_TOKEN=$CAIRN_TOKEN -- python3 -m cairnsandbox.mcp` (or export them first in the shell that launches Claude Code, and the child process inherits them).

### Codex / Cursor / others

The general shape of the configuration (file location and outer key names vary by client; the core is command + args + env):

```json
{
  "mcpServers": {
    "cairn": {
      "command": "python3",
      "args": ["-m", "cairnsandbox.mcp"],
      "env": { "CAIRN_URL": "http://cp:18700", "CAIRN_PROJECT": "acme" }
    }
  }
}
```

Do not put the token in the configuration; see the security notes below.

## Tools (12)

Every tool's result is a single text content block; "Returns" in the table below refers to the content of that text (mostly a JSON string). The `sandbox` parameter is a sandbox id.

| Tool | Required | Optional | Returns | Typical use |
|---|---|---|---|---|
| `sandbox_create` | none | `template` str, `mem` int (MiB), `cls` `persistent`\|`ephemeral`, `network` object (e.g. `{"allow_hosts":["pypi.org"]}`), `env` object, `ttl_idle_s` int, `template_key` str, `layers` [str] | `{"id","status","node"}` | First step of any job |
| `sandbox_run` | `sandbox`, `cmd` | `cwd`, `timeout` int, `user` `agent`\|`root`, `env` object | `{"exit","stdout","stderr","timed_out"}` | Run commands, tests, installs |
| `sandbox_write_file` | `sandbox`, `path`, `text` | — | `{"bytes": n}` | Write a whole text file (absolute path) |
| `sandbox_read_file` | `sandbox`, `path` | — | The raw file content (UTF-8, not JSON) | Read results / logs |
| `sandbox_checkpoint` | `sandbox` | `name`, `note` | Checkpoint dict (including `name`, `chain_len`) | Before a risky step |
| `sandbox_rollback` | `sandbox`, `checkpoint` | — | `{"id","status","rollback_s"}` | Go back if it went wrong |
| `sandbox_fork` | `sandbox` | `n` int (default 1), `checkpoint`, `cls` (default `ephemeral`) | Array of clone ids `["sb-…"]` | Try several approaches in parallel |
| `sandbox_pause` | `sandbox` | — | `{"status"}` | Pause to disk; the next command resumes it transparently |
| `sandbox_resume` | `sandbox` | — | `{"status"}` | Explicit resume |
| `sandbox_kill` | `sandbox` | — | `{"deleted": id}` | Delete when done |
| `sandbox_list` | none | — | `[{"id","status","node","class"}]` | See the sandboxes the token can see |
| `sandbox_info` | `sandbox` | — | The full sandbox dict (status, checkpoints, volumes, network, usage) | Troubleshooting |

The tool parameters are a subset of the SDK's: there is no `vcpu`, `disk`, `volumes`, `stdin`, `ttl_max_s`, `priority`, etc.; if you need those, use the [Python SDK](sdk.md).

## Protocol coverage

| Method | Behavior |
|---|---|
| `initialize` | Returns `protocolVersion: "2025-06-18"` (fixed, not negotiated), `capabilities: {tools: {}}`, `serverInfo {name: "cairnsandbox", version: "0.1.0"}`, and a piece of `instructions` |
| `notifications/initialized` | Recorded, no reply |
| `ping` | `{}` |
| `tools/list` | The 12 tools in the table above with their `inputSchema` |
| `tools/call` | See below |

There are no resources, prompts, logging, or sampling, and no streaming output or progress notifications: `sandbox_run` blocks until the command ends and then returns once. Other methods carrying an `id` return the JSON-RPC error `-32601 method not found`; unknown notifications without an `id` are silently ignored. Invalid JSON lines are discarded; an uncaught exception during handling replies `-32603`.

## How errors are returned

Failures inside a tool do not use a JSON-RPC error; they are a normal result with `isError: true`:

- The control plane returns an error status → the text is `"{status}: {message}"`, e.g. `404: no checkpoint`, `429: no node with capacity`, `402: quota: daily budget`; a transport failure is `0: transport: …`.
- A missing required argument → `ValueError: missing required argument(s): sandbox`; an unknown tool → `ValueError: unknown tool …`; other exceptions, such as reading a non-UTF-8 file → `"{exception class name}: {message}"`. All are `isError: true`.
- Note that in `sandbox_run`, the command itself exiting non-zero is **not** an error: `isError` is false, so look at `exit` in the JSON.

## Security notes

- Keep the token only in an environment variable (`CAIRN_TOKEN`); do not write it into configuration such as `.mcp.json` that gets committed to a repository; inject it through the shell profile, a secrets manager, or the client's machine-local private configuration.
- The token's permissions are the agent's permissions: give it a token scoped to one project (`CAIRN_PROJECT`) with a daily budget, and let quotas and the egress policy be the backstop.
- Egress is controlled by the sandbox network policy by default; `sandbox_create`'s `network` can be specified at creation time, but the MCP tools have no way to loosen the ingress of an existing sandbox (that requires owner approval, see [api/network.md](api/network.md)).
