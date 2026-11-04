/**
 * cairnbox: JavaScript / TypeScript SDK for the Cairn sandbox cloud (CONTRACT §1 over HTTP, no runtime dependencies).
 *
 *   import { Client } from "cairnbox";
 *   const c = new Client({ url: "http://127.0.0.1:18700", token: "..." });      // or CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
 *   const sb = await c.sandboxes.create({ mem: 512, network: { allow_hosts: ["pypi.org"] } });
 *   console.log((await sb.commands.run("python3 -V")).stdout);
 *   await sb.checkpoints.create("before");                                       // what E2B does not have: checkpoints, rollback, fork from a point
 *   await sb.kill();
 */
export { Client, ApiError, NotFound, Forbidden, NoCapacity, QuotaExceeded, NeedsApproval } from "./client.ts";
export type { ClientOptions, CreateOptions, Json, RequestOptions } from "./client.ts";
export { Sandbox, CommandResult, Session } from "./sandbox.ts";
export type { RunOptions, StreamEvent } from "./sandbox.ts";
