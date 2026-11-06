# Metering and billing (Metering)

This page covers context 7: the price list, usage events, statements (Statement), settlement (settle), today's spend, and per-sandbox usage. Other contexts do not compute money; they only report usage, which is priced here. For the contract see [CONTRACT §1](../../../../CONTRACT.md); for the domain design see [docs/002](../../../002-domain.md) (Chinese). When today's spend reaches the Project's `daily_usd` quota, creating a sandbox returns 402 ([tenancy.md](tenancy.md)). For calling conventions see [README](README.md).

## Usage and default prices

A tick runs once a minute (with multiple replicas, the replica holding the lease runs it): for every **running** sandbox it records `vcpu_s` and `mem_mib_s`; for every **running or paused** sandbox it records `disk_mib_s` (based on the `disk_used_mib` reported by the node, i.e. actual allocation); it reads cumulative usage from the model proxy and records the increments of `model_tokens_in|out|cached` per sandbox, with the cost decided by the proxy and placed on the `in` row; for egress it records the increment of `egress_bytes` from the node-reported cumulative bidirectional bytes of the egress proxy.

| kind | Unit | Default price (USD / unit) | Approximately |
| --- | --- | --- | --- |
| `vcpu_s` | vcpu × second (running) | 0.000014 | $0.0504 / vcpu·hour |
| `mem_mib_s` | MiB × second (running) | 0.0000000044 | $0.0162 / GiB·hour |
| `disk_mib_s` | writable-layer MiB × second (running or paused) | 0.00000000003 | $0.00011 / GiB·hour |
| `egress_bytes` | bytes (egress proxy, both directions) | 0.00000000004 | $0.04 / GiB |
| `vcpu_active_s` | CPU seconds the guest **actually used** (cgroup counter, D67) | **0 (not charged)** | see below |
| `model_tokens_in` | token | priced by the model proxy; the event carries its own `cost_usd` | |
| `model_tokens_out` | token | same as above | |
| `model_tokens_cached` | token | same as above | |

Event source (`source`): `tick` or `model_proxy`.

## GET /metering/prices

Caller. Response `{"version": 0, "per_unit": {"vcpu_s": 0.000014, …}, "created": 1767225600.0}`. When never changed, `version` is 0 and `per_unit` is the defaults in the table above.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/metering/prices
```

## PUT /metering/prices

**Admin only**. Request `{kind: usd_per_unit, …}`, merged with the current prices into a new version (`version` + 1). Response: the new price list. Errors: 400 if it is not a `{kind: number}` object or is empty; 403.

```bash
curl -s -X PUT $CP/metering/prices -H "Authorization: Bearer $TOKEN" -d '{"vcpu_s": 0.00002}'
```

## POST /metering/tick

**Admin only**. Manually triggers one tick (usually for testing). Response `{"events": n}`; on failure 500 `{"error", "events"}`.

## GET /metering/projects/\<p\>/statement

Caller (the account that owns this Project) or admin.

Query: `?from=<unix seconds>&to=<unix seconds>` (`to` omitted or 0 = now), `&format=csv` returns CSV.

Response:
```json
{"project": "web", "from": 1767139200, "to": 1767225600,
 "lines": [{"kind": "vcpu_s", "amount": 7200, "unit": "…", "cost_usd": 0.072, "events": 120}],
 "total_usd": 0.31, "sandboxes": 3}
```

`?format=csv` → `text/csv`, with `Content-Disposition: attachment; filename=<project>-<from>-<to>.csv`:
```
project,from,to,kind,unit,amount,cost_usd,events
web,1767139200,1767225600,vcpu_s,…,7200,0.072000,120
web,1767139200,1767225600,total,usd,,0.310000,3
```
One row per kind of usage, with a final `total` row. Error: 403 `not your project`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/metering/projects/web/statement?from=1767139200&format=csv"
```

## GET /metering/projects/\<p\>/today

Caller. Spend since local midnight (the quota's "today" window). Response `{"project": "web", "spent_usd": 0.12}`.

## POST /metering/projects/\<p\>/statements

Project owner or admin. **Freezes** the statement for a period into the ledger; later changes to prices or events do not affect it.

Request:
```json
{"from": 1767139200, "to": 1767225600}
```
Response **201**:
```json
{"id": "st-…", "project": "web", "from": 1767139200, "to": 1767225600, "total_usd": 0.31, "sandboxes": 3, "lines": […], "created": 1767229000.0}
```
Errors: 400 (overlaps an already settled period, or the period is invalid); 403 `owner only`.

```bash
curl -s -X POST $CP/metering/projects/web/statements -H "Authorization: Bearer $TOKEN" -d '{"from":1767139200,"to":1767225600}'
```

## GET /metering/projects/\<p\>/statements

Caller. Settled statements, newest first. Response `{"project": "web", "statements": […]}`.

## GET /metering/sandboxes/\<id\>

Caller (the account that owns the sandbox) or admin. The cumulative usage of this sandbox, summarized by kind. Response:
```json
{"sandbox": "sb-123", "usage": [{"kind": "vcpu_s", "amount": 3600, "unit": "…", "cost_usd": 0.0504, "events": 60}]}
```
Error: 403 `not your sandbox`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/metering/sandboxes/sb-123
```

## Prepaid credit (D59)

An account is **postpaid** by default (usage is recorded but not blocked). When set to **prepaid**: balance = cumulative top-ups − cumulative spend of all projects under the account; when the balance is ≤ 0, creating a sandbox returns **402 `quota: out of credit`**, and the control plane's per-minute sweep **suspends** the account (same as D53: running sandboxes are paused with state preserved, members can only read and clean up, with the reason `out of credit`); a top-up lifts it **immediately** (only this kind of suspension is lifted; an account suspended for abuse is not released by a top-up).

| Endpoint | Who | Effect |
|---|---|---|
| `PUT /accounts/<a>/billing` `{"mode": "prepaid" \| "postpaid"}` | Deployer | Switch the billing mode |
| `POST /accounts/<a>/credit` `{"usd": 20, "note": "invoice 42"}` | Deployer | Top up (called by the payment provider's webhook); negative = adjustment / refund; zero is 400; audit `credit` |
| `GET /accounts/<a>/balance` | owner / deployer | `{mode, credits_usd, spent_usd, balance_usd, entries: [last 20 entries]}` |

When the database cannot be read, the balance is treated as "sufficient" (a storage failure should not lock out all customers). **Payment collection itself (Stripe, etc.) is not in the control plane**: their webhooks are verified in your own small service, which then calls `/credit`, so payment secrets never enter the control plane.

## GET /metering/projects/\<p\>/breakdown

Where the money went (D66). `?by=sandbox` (default) groups by sandbox: the top 50 most expensive sandboxes, with the rest merged into `other`; `?by=day` groups by UTC day (earliest to latest; days with no usage do not appear, so the caller fills in zeros). `?from=` / `?to=` are unix seconds (`to` omitted = now). Response: `{"project": "acme", "by": "day", "rows": [{"key": "1767225600", "cost_usd": 1.5, "kinds": {"vcpu_s": 1.0, "mem_mib_s": 0.5}}], "total_usd": 1.5}`; with `by=day`, `key` is the first second (unix) of that day. Members can only see their own projects. The console's "Usage and billing" page uses this for its daily bar chart and its "Most expensive sandboxes" table.

## Billing by active CPU (optional, D67)

By default CPU is charged by **allocation** (`vcpu_s`, same as E2B / Daytona). An agent sandbox that spends most of its time waiting for model replies uses far less CPU than it is allocated; Vercel charges by "active CPU". We support both measures: the node reports `cpu_usec` in the sandbox info (cumulative CPU microseconds used by the guest cgroup), and metering takes the difference from the previous reading every minute and records it as `vcpu_active_s`. **The default price is 0, and when the price is 0 nothing is recorded at all** (no usage events are added); to bill by active CPU, save a new price list: `PUT /metering/prices {"vcpu_active_s": 0.00004, "vcpu_s": 0}` (the active-CPU unit price is usually higher than the allocation unit price, because only the part actually used is charged). Edge cases: resuming after a pause rebuilds the cgroup and the counter starts from 0, handled as "a decrease = a reset"; if the sandbox has already been running for more than 5 minutes when metering first sees it, only a baseline is set and nothing is back-charged; if the node has not been upgraded to a version that provides `cpu_usec`, or the sandbox is not running under the jailer, there is no such reading.
