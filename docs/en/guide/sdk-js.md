# JavaScript / TypeScript SDK Reference

Source: [client.ts](../../../sdk/js/src/client.ts) (Client and the top-level collections), [sandbox.ts](../../../sdk/js/src/sandbox.ts) (a single sandbox). The shape corresponds one to one with the [Python SDK](sdk.md) (same names; options use camelCase / wire fields stay snake_case). Zero runtime dependencies, uses `fetch` (Node ≥ 18, Deno, Bun, browsers).

## Installation and configuration

```
cd sdk/js && npm install && npm run build      # produces dist/; npm pack gives an installable package
npm install ./cairnsandbox-0.1.0.tgz
```

| Environment variable | Purpose | Default |
|---|---|---|
| `CAIRN_URL` | Control plane address | `http://127.0.0.1:18700` |
| `CAIRN_TOKEN` | Bearer token | empty |
| `CAIRN_PROJECT` | Project (`X-Project`) | empty (the token's default project) |

```ts
import { Client } from "cairnsandbox";
const c = new Client({ url: "http://cp:18700", token: "…", project: "acme" });   // or just read environment variables: new Client()
const sb = await c.sandboxes.create({ mem: 512, network: { allow_hosts: ["pypi.org"] } });
const r = await sb.commands.run("python3 -V", { timeout: 30 });
console.log(r.stdout);
await sb.files.write("/home/agent/hello.txt", "hi");
await sb.checkpoints.create("before");            // things E2B lacks: checkpoints, rollback, fork from a checkpoint
await sb.commands.run("rm /home/agent/hello.txt");
await sb.checkpoints.rollback("before");
for await (const ev of sb.commands.stream("for i in 1 2 3; do echo $i; sleep 1; done")) {
  if (ev.type === "done") console.log("exit", ev.result.exit); else process.stdout.write(ev.data);
}
await sb.kill();
```

## Differences from the Python SDK

| Python | JS/TS |
|---|---|
| `sb.commands.stream(...)` yields `(kind, text)` tuples | An async generator that yields `{type:"stdout"\|"stderr", data}`, with the last one being `{type:"done", result}` |
| `Client(..., timeout=600)` (seconds) | `new Client({ timeoutMs: 600000 })` |
| `sb.events()` returns `(events, last_seq)` | Returns `{ events, lastSeq }` |
| `ports.get()` / `ports.websocket()` | `ports.fetch()`; for WebSocket, use `ports.url()` + `ports.headers()` with a ws library yourself (the SDK ships no WebSocket implementation) |
| `with Sandbox` automatically calls `kill` | None (JS has no equivalent syntax); use `try / finally` |

## Errors

`ApiError` (`.status`, `.body`) and its subclasses: `NotFound` (404), `Forbidden` (403, including a disabled account), `NoCapacity` (429), `QuotaExceeded` (402), `NeedsApproval` (202, `.request` is the pending relaxation). Failing to connect is an `ApiError` with `status === 0`.

## Testing

**Smoke test against a real control plane**: `sdk/js/e2e/e2e.ts` (with `CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT` set, run `node e2e/e2e.ts`; Node ≥ 22.18 runs TypeScript directly): creates a small sandbox, runs a command, streams output, reads and writes files, does a checkpoint rollback, pauses and resumes, and finally deletes it. It has passed all 15 items against the control plane on the dev machine using an **API key** (D56).

`npm run check` (type check + start a fake control plane and run 8 test cases). In CI use `docker build --target check sdk/js`.
