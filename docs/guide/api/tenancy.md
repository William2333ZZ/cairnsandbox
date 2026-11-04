# 租户与访问(Tenancy)

本文覆盖上下文 1:账户(Account)、项目(Project)、成员(Member)与令牌、配额(Quota)、项目默认网络策略,以及 `/me` 和 `/login`。关系是 Account ⊃ Project ⊃ Member;沙箱、模板、卷归 Project,账单和配额按 Project 算。合同见 [CONTRACT §1](../../../CONTRACT.md),领域设计见 [docs/002](../../002-domain.md),调用约定见 [README](README.md)。

id 规则(账户、Project、成员的 `user`):小写字母、数字、`_`、`-`,2–32 个字符(见 `tenancy.go`)。成员令牌只存哈希,创建和轮换时**只返回一次**。

## GET /me

任何已认证调用者。

响应:
```json
{"user": "alice", "admin": false, "auth": true, "account": "acme", "role": "owner", "projects": ["acme", "web"]}
```
`auth` 表示控制面是否开启了鉴权。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/me
```

## POST /login

免鉴权(给工作台校验令牌用)。

请求:`{"token": "…"}`。响应:`{"ok": true, "auth": true, "user": "alice", "admin": false}`。令牌无效 → **401** `{"ok": false, "user": null, …}`。

```bash
curl -s -X POST $CP/login -d '{"token":"'$TOKEN'"}'
```

## GET /accounts

admin 看全部账户;成员只看自己的。响应 `{"accounts": [{id, name, created}]}`。错误:404 `no account`(成员的账户不存在)。

```bash
curl -s -H "Authorization: Bearer $TOKEN" $CP/accounts
```

## POST /accounts

仅 admin。创建账户,自动带一个名为 `default`、id 等于账户 id 的 Project。

请求:
```json
{"id": "acme", "name": "Acme Inc"}
```
响应 **201** `{id, name, created}`。错误:400 id 不合法或账户已存在;403 非 admin。

```bash
curl -s -X POST $CP/accounts -H "Authorization: Bearer $TOKEN" -d '{"id":"acme","name":"Acme"}'
```

## GET /accounts/\<a\>/projects

本账户的成员或 admin。响应 `{"projects": [{id, account, name, created}]}`。错误:403 `not your account`。

## POST /accounts/\<a\>/projects

本账户的 owner 或 admin。

请求:
```json
{"id": "web?", "name": "Web"}
```
`id` 可省(`?` 表示可选;省略时的取值规则见 CONTRACT §1)。响应 **201** `{id, account, name, created}`。错误:400 id 不合法或已存在;403 `owner only` / `not your account`。

```bash
curl -s -X POST $CP/accounts/acme/projects -H "Authorization: Bearer $TOKEN" -d '{"id":"web","name":"Web"}'
```

## GET /projects/\<p\>/members

本账户任一成员或 admin。响应 `{"members": [{id, account, user, name, role, created}]}`(不含令牌)。错误:404 `no project`;403。

## POST /projects/\<p\>/members

owner 或 admin。给 Project 所在的账户加一个成员。

请求:
```json
{"user": "bob", "name?": "Bob", "role": "member"}
```
`role`:`owner` 或 `member`。响应 **201**:
```json
{"id": "…", "account": "acme", "user": "bob", "name": "Bob", "role": "member", "token": "<只出现这一次>"}
```
错误:400(user 不合法 / 已存在 / role 不是 owner|member);403 `owner only`;404 `no project`。

```bash
curl -s -X POST $CP/projects/web/members -H "Authorization: Bearer $TOKEN" -d '{"user":"bob","role":"member"}'
```

## DELETE /projects/\<p\>/members/\<user\>

owner 或 admin。响应 `{"deleted": "bob"}`。错误:404 `no such member`;403。成员的令牌随之失效。

## POST /projects/\<p\>/members/\<u\>/rotate

账户 owner、admin,或成员本人。生成新令牌,旧令牌立刻失效。

请求:`{"ttl_s?": 86400}`(给新令牌一个寿命,过期后调用得 401;省略则不过期)。响应 **200**:
```json
{"account": "acme", "user": "bob", "token": "<新令牌,只出现这一次>", "expires": 1767225600.0}
```
错误:403 `the owner or the member themselves`;404 `no project`;400。每次轮换写审计 `member_rotate`。

```bash
curl -s -X POST $CP/projects/web/members/bob/rotate -H "Authorization: Bearer $TOKEN" -d '{"ttl_s":86400}'
```

## POST /projects/\<p\>/members/\<u\>/expiry

权限同 rotate。给**现有**令牌设寿命。

请求:`{"ttl_s": 3600}`,`0` = 不过期。响应 `{"user": "bob", "ttl_s": 3600}`。错误:404 `no such member`;403。写审计 `member_expiry`。

## GET /projects/\<p\>/quota

本账户成员或 admin。响应:
```json
{"project": "web", "max_sandboxes": 20, "max_storage_gib": 100, "daily_usd": 5}
```
三项都是 `0` = 不限。

## PUT /projects/\<p\>/quota

owner 或 admin。请求同上形状(`project` 可省):`{"max_sandboxes": 20, "max_storage_gib": 100, "daily_usd": 5}`。响应:新配额。错误:403 `owner only` / `not your project`;400。

配额在**创建沙箱**时准入(见 [sandboxes.md](sandboxes.md)):

| 超限 | 响应 |
| --- | --- |
| 沙箱数 | 429 `{"error":"quota: sandboxes"}` |
| 存储(本项目沙箱可写层分配之和 + 卷大小之和 + 这次要的 disk) | 429 `{"error":"quota: storage", "used_mib", "asked_mib"}` |
| 当日花费 | 402 `{"error":"quota: daily budget", "project", "daily_usd", "spent_usd"}` |

```bash
curl -s -X PUT $CP/projects/web/quota -H "Authorization: Bearer $TOKEN" -d '{"max_sandboxes":20,"max_storage_gib":100,"daily_usd":5}'
```

## GET /projects/\<p\>/network

本账户成员或 admin。Project 的默认网络策略。响应 `{"project": "web", "network": {…} | null}`。

## PUT /projects/\<p\>/network

owner 或 admin。创建沙箱的请求**没带** `network` 时用它;带了以请求为准。

请求:`{"network": {mode?, allow_hosts?, allow_direct?, rules?, limits?}}`,也可直接把策略对象作为 body。形状与规则语法见 [network.md](network.md)。响应 `{"project": "web", "network": {…}}`。错误:400 策略不合法;403 `owner only`。写审计 `project_network`。

```bash
curl -s -X PUT $CP/projects/web/network -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"allowlist","allow_hosts":["pypi.org","files.pythonhosted.org"]}'
```

## DELETE /projects/\<p\>/network

owner 或 admin。删掉默认,响应 `{"project": "web", "network": null}`。

## GET /projects/\<p\>/keys · POST · DELETE /projects/\<p\>/keys/\<id\>

API 密钥(D56):给 CI、脚本、看板用的**命名凭证**,不是人的令牌。仅项目 owner(和部署者)能管理;密钥自己不能再发密钥。

`POST` 请求 `{"name": "ci", "role": "member" | "viewer", "ttl_s": 2592000}`(`role` 默认 member,**没有 owner 密钥**;`ttl_s` 省略 = 不过期)。响应 201:`{"key": {id, account, project, name, role, created, expires, last_used, created_by}, "secret": "cairn_…"}`——**secret 只在这一次返回**,库里只存哈希。

密钥的行为:只能访问它所属的这一个项目(指定别的项目 403,列表只含本项目);`viewer` 只读——任何非 GET / HEAD 请求、以及 WebSocket 升级(是能写的 GET)回 403 `read-only credential`,E2B 端口上也一样;账户被停用时同样受限;`/me` 里 `key: true`。`GET` 列出(不含 secret,有 `last_used`,每分钟最多更新一次);`DELETE /projects/<p>/keys/<id>` 立即撤销(下一个请求就 401),不存在 404。审计 `key_create` / `key_revoke`。

```bash
curl -s -X POST -H "Authorization: Bearer $OWNER" -d '{"name":"ci","role":"member","ttl_s":2592000}' $CP/projects/acme-web/keys
curl -s -H "Authorization: Bearer cairn_…" $CP/sandboxes       # 用密钥
```

## POST /invites · GET /invites · POST /signup

邀请制自助开户(D60)。**部署者**用 `POST /invites` 发一次性邀请码:`{"note": "beta 第一批", "credit_usd": 5, "max_sandboxes": 3, "daily_usd": 2, "ttl_s": 604800}`(全部可选;`credit_usd` > 0 时新账户自动变**预付**并入账这笔额度;配额写进新账户的默认项目)。响应 201:`{"invite": {...}, "code": "inv_…"}`——**code 只返回这一次**,库里只存哈希。`GET /invites` 列出(不含 code,有 `used_by` / `used_at`)。

**任何人**(不带 bearer)凭邀请码 `POST /signup`:`{"code": "inv_…", "account": "acme", "user": "alice", "name": "Alice"}`。成功 201:`{"account": "acme", "project": "acme", "user": "alice", "token": "…", "credit_usd": 5}`——alice 是这个账户的 **owner**,token 只显示一次。失败:邀请码无效 / 已用 / 过期一律 403 `invite code is not valid`(不让人探测);名字不合规或已被占用 400(**邀请码会被放回**,可以换名字重试);同一来源地址每分钟最多 10 次,超出 429。审计 `invite_create` / `signup`。

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" -d '{"note":"wave 1","credit_usd":5}' $CP/invites
curl -s -X POST -d '{"code":"inv_…","account":"acme","user":"alice"}' $CP/signup      # 无需令牌
```

控制台登录页有"有邀请码?创建账户"入口,创建后直接给出令牌并登录。

## DELETE /accounts/\<a\>

仅部署者。**下线一个账户**(D68):删掉它的成员、API 密钥、项目(连同配额与默认网络)、停用与计费方式记录。账户下还有沙箱或卷时返回 **409**(带数量),加 `?force=1` 才会连沙箱和卷一起删。**保留**:用量事件、额度账本、已结算账单和审计(这些是账目证据)。账户名**保留不再发放**(账本与用量按名字记,下一个注册同名的人会继承它们)。默认账户不能删。审计 `account_delete`。响应 200:`{"account": "acme", "deleted": true, "sandboxes_deleted": 2, "volumes_deleted": 1, "removed": {"members": 3, "api_keys": 1, …}, "kept": "…"}`。

## POST /accounts/\<a\>/suspend

仅部署者。停用一个账户(滥用处置,D53)。请求 `{"reason": "…"}`,**reason 必填**(账户成员会看到它)。效果:①记下停用;②把账户下所有项目里**运行中**的沙箱暂停(状态保留,算力与出口停止);③该账户的成员此后只能**查看**(沙箱列表与单台信息,以及它的检查点 / egress / metrics 子项)和 **`DELETE`** 清理——不能新建、恢复、执行、改网络,**也不能读文件、事件、端口或开 WebSocket**(节点会在这些请求上把暂停的沙箱自动恢复,放行等于解除暂停),E2B 那个端口也一样,返回 403 `account suspended`;④`/me` 多一个 `suspended` 字段,控制台顶部出红色提示。部署者自己不受限(便于调查)。

响应 200:`{"account": "acme", "suspended": true, "reason": "…", "paused": ["sb-…"], "pause_failed": []}`。账户不存在 404。审计 `account_suspend`。

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN" -d '{"reason":"crypto mining, see ticket 123"}' $CP/accounts/acme/suspend
```

## POST /accounts/\<a\>/unsuspend

仅部署者。解除停用;**不会**自动恢复被暂停的沙箱,由 owner 自己决定恢复哪些。没有被停用时 404。审计 `account_unsuspend`。

## GET /suspensions

仅部署者。`{"suspensions": [{account, reason, by, at}]}`。指标 `cairn_accounts_suspended`。

## 旧的 /users

合同 §8 的多用户接口,等价于 `default` 账户的成员。仅 admin。

| 端点 | 说明 |
| --- | --- |
| `GET /users` | `{"users": […]}` |
| `POST /users {user, name}` | **201** `{user, name, token}`,令牌只返回一次;400 校验失败 |
| `DELETE /users/<user>` | `{"deleted": "<user>"}`;404 `no such user` |

新代码用 `/projects/<p>/members`。
