# 计量与账单(Metering)

本文覆盖上下文 7:价目表、用量事件、账单(Statement)、结算(settle)、当日花费、单台沙箱用量。其他上下文不算钱,只上报用量,由这里定价。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md)。当日花费到 Project 的 `daily_usd` 配额时创建沙箱回 402([tenancy.md](tenancy.md))。调用约定见 [README](README.md)。

## 用量与默认价格

每分钟一次 tick(多副本时一个副本持租约执行):给每台**运行中**的沙箱记 `vcpu_s`、`mem_mib_s`;**运行或暂停**的记 `disk_mib_s`(按节点报的 `disk_used_mib`,即实际分配);从模型代理读累计用量,按沙箱记 `model_tokens_in|out|cached` 的增量,费用由代理定、落在 `in` 那一行;出站按节点报的出口代理双向累计字节记 `egress_bytes` 的增量。

| kind | 单位 | 默认价格(USD / 单位) | 约合 |
| --- | --- | --- | --- |
| `vcpu_s` | vcpu × 秒(运行) | 0.000014 | $0.0504 / vcpu·小时 |
| `mem_mib_s` | MiB × 秒(运行) | 0.0000000044 | $0.0162 / GiB·小时 |
| `disk_mib_s` | 可写层 MiB × 秒(运行或暂停) | 0.00000000003 | $0.00011 / GiB·小时 |
| `egress_bytes` | 字节(出口代理,双向) | 0.00000000004 | $0.04 / GiB |
| `vcpu_active_s` | 客户机**真正用掉**的 CPU 秒(cgroup 计数,D67) | **0(不计)** | 见下 |
| `model_tokens_in` | token | 由模型代理定价,事件自带 `cost_usd` | |
| `model_tokens_out` | token | 同上 | |
| `model_tokens_cached` | token | 同上 | |

事件来源(`source`):`tick` 或 `model_proxy`。

## GET /metering/prices

调用者。响应 `{"version": 0, "per_unit": {"vcpu_s": 0.000014, …}, "created": 1767225600.0}`。没改过时 `version` 为 0,`per_unit` 是上表默认值。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/metering/prices
```

## PUT /metering/prices

**仅 admin**。请求 `{kind: usd_per_unit, …}`,与现行价格合并成新版本(`version` + 1)。响应:新价目表。错误:400 不是 `{kind: 数}` 对象或为空;403。

```bash
curl -s -X PUT $CP/metering/prices -H "Authorization: Bearer $TOKEN" -d '{"vcpu_s": 0.00002}'
```

## POST /metering/tick

**仅 admin**。手动触发一次 tick(通常用于测试)。响应 `{"events": n}`;失败 500 `{"error", "events"}`。

## GET /metering/projects/\<p\>/statement

调用者(本 Project 所在账户)或 admin。

查询:`?from=<unix 秒>&to=<unix 秒>`(`to` 省略或 0 = 现在),`&format=csv` 返回 CSV。

响应:
```json
{"project": "web", "from": 1767139200, "to": 1767225600,
 "lines": [{"kind": "vcpu_s", "amount": 7200, "unit": "…", "cost_usd": 0.072, "events": 120}],
 "total_usd": 0.31, "sandboxes": 3}
```

`?format=csv` → `text/csv`,带 `Content-Disposition: attachment; filename=<project>-<from>-<to>.csv`:
```
project,from,to,kind,unit,amount,cost_usd,events
web,1767139200,1767225600,vcpu_s,…,7200,0.072000,120
web,1767139200,1767225600,total,usd,,0.310000,3
```
每种用量一行,最后一行 `total`。错误:403 `not your project`。

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/metering/projects/web/statement?from=1767139200&format=csv"
```

## GET /metering/projects/\<p\>/today

调用者。本地午夜起的花费(配额的"当日"窗口)。响应 `{"project": "web", "spent_usd": 0.12}`。

## POST /metering/projects/\<p\>/statements

Project owner 或 admin。把一段时间的账单**冻结**进账本;之后改价格或事件也不影响它。

请求:
```json
{"from": 1767139200, "to": 1767225600}
```
响应 **201**:
```json
{"id": "st-…", "project": "web", "from": 1767139200, "to": 1767225600, "total_usd": 0.31, "sandboxes": 3, "lines": […], "created": 1767229000.0}
```
错误:400(与已结算的时段重叠,或时段不合法);403 `owner only`。

```bash
curl -s -X POST $CP/metering/projects/web/statements -H "Authorization: Bearer $TOKEN" -d '{"from":1767139200,"to":1767225600}'
```

## GET /metering/projects/\<p\>/statements

调用者。已结算的账单,新的在前。响应 `{"project": "web", "statements": […]}`。

## GET /metering/sandboxes/\<id\>

调用者(沙箱所属账户)或 admin。这台沙箱累计的用量,按 kind 汇总。响应:
```json
{"sandbox": "sb-123", "usage": [{"kind": "vcpu_s", "amount": 3600, "unit": "…", "cost_usd": 0.0504, "events": 60}]}
```
错误:403 `not your sandbox`。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/metering/sandboxes/sb-123
```

## 预付额度(D59)

账户默认是**后付**(只记账,不拦)。设成**预付**后:余额 = 累计充值 − 账户下所有项目的累计花费;余额 ≤ 0 时创建返回 **402 `quota: out of credit`**,并且控制面每分钟的巡检会把账户**停用**(同 D53:运行中的沙箱暂停、状态保留,成员只能读和清理,原因写 `out of credit`),充值后**立刻**解除(只解除这一种停用,因滥用被停的账户不会因为充值而放开)。

| 接口 | 谁 | 作用 |
|---|---|---|
| `PUT /accounts/<a>/billing` `{"mode": "prepaid" \| "postpaid"}` | 部署者 | 切换计费方式 |
| `POST /accounts/<a>/credit` `{"usd": 20, "note": "invoice 42"}` | 部署者 | 充值(支付服务商的 webhook 调这个);负数 = 调整 / 退款;零 400;审计 `credit` |
| `GET /accounts/<a>/balance` | owner / 部署者 | `{mode, credits_usd, spent_usd, balance_usd, entries: [最近 20 笔]}` |

数据库读不到时,余额当作"充足"(存储故障不该把所有客户拒之门外)。**收款本身(Stripe 等)不在控制面里**:它们的 webhook 在你自己的小服务里验签后调 `/credit`,这样支付密钥永远不进控制面。

## GET /metering/projects/\<p\>/breakdown

钱花在哪(D66)。`?by=sandbox`(默认)按沙箱分:花得最多的前 50 台,其余合并成 `other`;`?by=day` 按 UTC 日分(从早到晚,没有用量的日子不出现,调用方自己补零)。`?from=` / `?to=` 是 unix 秒(`to` 省略 = 现在)。响应:`{"project": "acme", "by": "day", "rows": [{"key": "1767225600", "cost_usd": 1.5, "kinds": {"vcpu_s": 1.0, "mem_mib_s": 0.5}}], "total_usd": 1.5}`;`by=day` 时 `key` 是该日第一秒(unix)。成员只能看自己项目。控制台"用量与账单"页的每日柱状图和"花得最多的沙箱"表用的就是它。

## 按活跃 CPU 计费(可选,D67)

默认按**分配**计 CPU(`vcpu_s`,同 E2B / Daytona)。大部分时间在等模型回复的 agent 沙箱,实际用掉的 CPU 远少于分到的——Vercel 就按"活跃 CPU"计。我们把两种口径都做了:节点在沙箱信息里给出 `cpu_usec`(客户机 cgroup 累计使用的 CPU 微秒),计量每分钟取与上次的差记成 `vcpu_active_s`。**默认价为 0,价为 0 时根本不记**(不增加用量事件);想按活跃 CPU 计,保存一份新价目表:`PUT /metering/prices {"vcpu_active_s": 0.00004, "vcpu_s": 0}`(活跃 CPU 单价通常高于分配单价,因为同一时间只收用掉的部分)。边界:暂停后恢复会重建 cgroup、计数从 0 开始,按"回退 = 重置"处理;计量开始看这台沙箱时它已经跑了 5 分钟以上,只建基线不补收;节点没升级到带 `cpu_usec` 的版本、或沙箱没跑在 jailer 里,就没有这个读数。
