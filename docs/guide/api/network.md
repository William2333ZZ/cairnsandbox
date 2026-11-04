# 网络与出口(Network)

本文覆盖上下文 6:沙箱出口策略的形状与规则语法、节点上的凭证保险库、以及"放宽要审批"的请求队列。执行(nftables、出口代理、审计环)在节点上,控制面只在边上校验策略的形状,并把凭证推到每个节点。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md),威胁模型见 [docs/013](../../013-threat-model.md)。设置策略的端点:创建沙箱的 `network`、`PUT /sandboxes/<id>/network`([sandboxes.md](sandboxes.md))、`PUT /projects/<p>/network`([tenancy.md](tenancy.md))。调用约定见 [README](README.md)。

## 策略与规则语法

```json
{
  "mode": "allowlist",
  "allow_hosts": ["pypi.org", "*.pythonhosted.org"],
  "allow_direct": [],
  "rules": [
    {"name": "gh-read", "match": {"scheme": "https", "host": "api.github.com", "method": ["GET"], "path": "/repos/*"},
     "action": {"allow": true, "inject": "github"}}
  ],
  "limits": {"requests_per_min": 600, "max_bytes": 104857600}
}
```

| 字段 | 含义 |
| --- | --- |
| `mode` | `closed`(默认全拒)/ `allowlist`(按 `allow_hosts`)/ `audited`(放行并全记录)。别的值 → 400 |
| `allow_hosts` | 字符串列表;没有规则命中时,audited 放行,否则只放这里列的(空 = 全拒) |
| `allow_direct` | 字符串列表;直连地址 |
| `rules` | 按序,**第一条命中为准**;每条见下表 |
| `limits` | `{requests_per_min?, max_bytes?}`,非负整数,别的 key → 400。每台沙箱经出口代理的请求数 / 分钟与累计字节上限;超了代理回 429(审计 `egress_limited`) |

规则:

| 字段 | 取值 |
| --- | --- |
| `name` | 匹配 `[a-z0-9][a-z0-9_.-]{0,39}`;省略则为 `rule<i>` |
| `match.scheme?` | `http` 或 `https` |
| `match.host?` | `"精确"`、`"*.后缀"`、glob,或 `"~正则"`(`~` 开头,**整名匹配,不分大小写**) |
| `match.method?` | 方法列表,如 `["GET","POST"]` |
| `match.path?` | `"/精确"` 或 `"/前缀/*"`,必须以 `/` 开头 |
| `action.allow` | **必填**,bool |
| `action.inject?` | 保险库里的凭证名;把该凭证的头加到**明文 HTTP** 请求上,值不进沙箱。只能用在 `allow:true` 的规则上 |

`method` 与 `path` 只对明文 HTTP 生效;HTTPS 的 CONNECT 隧道只看得到 host 与端口。坏规则在控制面和节点都回 400。

## 被拒时的提示

出口代理拒绝请求时,HTTP 状态行的原因短语就是一句可操作的话(Go 的 `go install`、curl 都会把它打印出来):

```
403 egress denied for goproxy.cn by the sandbox network policy; to allow it: PUT /sandboxes/sb-123/network {"allow_hosts":["goproxy.cn"]}
```

成员发这个 PUT 会得到 202 和一条待批请求,owner / admin 批准后生效(见下)。(D79)

## "放宽"是什么意思

成员对 `PUT /sandboxes/<id>/network` 的请求,若相对沙箱**当前**策略满足下面任一条,就算放宽,不立即生效而是进审批([sandboxes.md](sandboxes.md) 的 202 流程)。判定是 `network.Relaxes`(`cp/internal/network/requests.go`):

| 放宽 | 例 |
| --- | --- |
| 更松的 `mode`(`closed` < `allowlist` < `audited`) | allowlist → audited |
| `allow_hosts` / `allow_direct` 里有当前没有的项 | 新增 `github.com` |
| 新的 allow 规则(当前规则里没有深度相等的这条) | 新增一条 `allow:true` |
| 撤掉一条 deny 规则(当前有的 `allow:false` 规则在新的 `rules` 里不见了) | |
| 调高或去掉 `limits`(当前设了的上限,新值更大或为 0) | `requests_per_min` 600 → 6000 |

没带的键不比较(节点按键合并)。收紧(其余情况)立即生效。owner 和 admin 的请求不走审批。

## GET /network/requests

调用者。列出本账户 Project 的放宽请求;admin 看全部。默认只返回 `pending`,`?all=1`(任何非空值)返回含已决定的。新的在前。

响应:
```json
{"requests": [{"id": "nr-…", "sandbox": "sb-123", "project": "web", "by": "bob", "network": {…}, "created": 1767225600.0, "status": "pending"}]}
```
已决定的多 `decided_by`、`decided`。

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CP/network/requests?all=1"
```

## POST /network/requests/\<id\>/approve

项目 owner 或 admin。批准时才把申请的策略推到节点(`PUT /sandboxes/<id>/network`);节点拒绝则原样返回节点状态码,`{"error":"the node refused the policy","detail":…}`,请求仍是 pending。成功响应 `{"request": {…, "status": "approved"}}`,写审计 `network_relax_approved`。

错误:403 `an owner of the project decides`;404 `no such request`;409 `already approved|denied`。

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $CP/network/requests/nr-abc123/approve
```

## POST /network/requests/\<id\>/deny

权限同上。不动节点。响应 `{"request": {…, "status": "denied"}}`,审计 `network_relax_denied`。已决定 → 409。

## 凭证保险库(仅 admin)

凭证的**值只存在节点**(`state/credentials.json`,0600,或 `EGRESS_CREDENTIALS_FILE`);控制面把值推到每个活节点后不留,审计只记名字。凭证名匹配 `[a-z0-9][a-z0-9_.-]{0,39}`,头名只含字母、数字、`-`。

### GET /network/credentials

响应 `{"nodes": {"node-a": ["github", "openai"], "node-b": ["github"]}}`——哪台节点有哪些名字(后加入的节点可能缺)。非 admin → 403。

### PUT /network/credentials/\<name\>

请求:
```json
{"header": "Authorization", "value": "Bearer ghp_…"}
```
响应 `{"name": "github", "header": "Authorization", "nodes": {"node-a": "ok", "node-b": "unreachable: …"}}`。错误:400(名字 / 头不合法,`value` 为空);403。之后在规则里写 `"inject": "github"` 使用。

```bash
curl -s -X PUT $CP/network/credentials/github -H "Authorization: Bearer $TOKEN" \
  -d '{"header":"Authorization","value":"Bearer ghp_xxx"}'
```

### DELETE /network/credentials/\<name\>

响应 `{"name": "github", "nodes": {"node-a": "ok"}}`(失败的节点为 `"failed"`)。

## GET /sandboxes/\<id\>/egress

最近 200 条出站决定,见 [sandboxes.md](sandboxes.md)。
