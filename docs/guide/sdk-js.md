# JavaScript / TypeScript SDK 参考

源码:[client.ts](../../sdk/js/src/client.ts)(Client 与顶层集合)、[sandbox.ts](../../sdk/js/src/sandbox.ts)(单个沙箱)。形状与 [Python SDK](sdk.md) 一一对应(名字相同,选项用 camelCase / 线上字段保持 snake_case)。运行时零依赖,用 `fetch`(Node ≥ 18、Deno、Bun、浏览器)。

## 安装与配置

```
cd sdk/js && npm install && npm run build      # 产出 dist/;npm pack 得到可安装的包
npm install ./cairnsandbox-0.1.0.tgz
```

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `CAIRN_URL` | 控制面地址 | `http://127.0.0.1:18700` |
| `CAIRN_TOKEN` | Bearer 令牌 | 空 |
| `CAIRN_PROJECT` | 项目(`X-Project`) | 空(令牌的默认项目) |

```ts
import { Client } from "cairnsandbox";
const c = new Client({ url: "http://cp:18700", token: "…", project: "acme" });   // 或只读环境变量:new Client()
const sb = await c.sandboxes.create({ mem: 512, network: { allow_hosts: ["pypi.org"] } });
const r = await sb.commands.run("python3 -V", { timeout: 30 });
console.log(r.stdout);
await sb.files.write("/home/agent/hello.txt", "hi");
await sb.checkpoints.create("before");            // E2B 没有的:检查点、回滚、从检查点 fork
await sb.commands.run("rm /home/agent/hello.txt");
await sb.checkpoints.rollback("before");
for await (const ev of sb.commands.stream("for i in 1 2 3; do echo $i; sleep 1; done")) {
  if (ev.type === "done") console.log("exit", ev.result.exit); else process.stdout.write(ev.data);
}
await sb.kill();
```

## 与 Python SDK 的差别

| Python | JS/TS |
|---|---|
| `sb.commands.stream(...)` 产出 `(kind, text)` 元组 | 异步生成器,产出 `{type:"stdout"\|"stderr", data}`,最后一个是 `{type:"done", result}` |
| `Client(..., timeout=600)`(秒) | `new Client({ timeoutMs: 600000 })` |
| `sb.events()` 返回 `(events, last_seq)` | 返回 `{ events, lastSeq }` |
| `ports.get()` / `ports.websocket()` | `ports.fetch()`;WebSocket 请自己用 `ports.url()` + `ports.headers()` 配合 ws 库(SDK 不带 WebSocket 实现) |
| `with Sandbox` 自动 `kill` | 没有(JS 没有等价语法),请 `try / finally` |

## 错误

`ApiError`(`.status`、`.body`)及子类:`NotFound`(404)、`Forbidden`(403,含账户被停用)、`NoCapacity`(429)、`QuotaExceeded`(402)、`NeedsApproval`(202,`.request` 是待批的放宽)。连不上是 `ApiError`,`status === 0`。

## 测试

**对真实控制面的冒烟**:`sdk/js/e2e/e2e.ts`(`CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT` 设好后 `node e2e/e2e.ts`,Node ≥ 22.18 直接跑 TypeScript):建一台小沙箱,跑命令、流式输出、文件读写、检查点回滚、暂停恢复,最后删掉。已在开发机的控制面上用一把 **API 密钥**(D56)跑通 15 项。

`npm run check`(类型检查 + 起一个假控制面跑 8 个用例)。CI 里用 `docker build --target check sdk/js`。
