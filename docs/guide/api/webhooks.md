# Webhooks(项目事件推送)

把项目里发生的事**推**到你的 HTTPS 地址(D72),不用轮询。仅项目 owner(和部署者)能管理;密钥(API key)和成员不行。调用约定见 [README](README.md)。

## 事件

| 事件 | 什么时候 | `data` |
| --- | --- | --- |
| `sandbox.created` | 沙箱创建成功 | `sandbox`、`node` |
| `network.approval_requested` | 有成员申请放宽某台沙箱的出口,等 owner 批 | `sandbox`、`request` |
| `build.finished` | 一次构建结束(成功或失败) | `build`、`kind`、`ok`、`error` |
| `account.suspended` / `account.unsuspended` | 账户被停用 / 解除(含额度用完) | `account`、`reason`(停用时) |
| `account.credit` | 账户记了一笔充值或调整 | `account`、`usd` |
| `ping` | 你点了"测试" | `message` |

账户级事件发给该账户**每个项目**里订阅了它的 webhook。

## 注册

`POST /projects/<p>/webhooks`,请求 `{"url": "https://hooks.example.com/aios", "events": ["sandbox.created", "build.finished"]}`(`events` 省略或空 = 全部)。响应 201:`{"webhook": {id, url, events, …}, "secret": "whsec_…"}`——**签名密钥只返回这一次**。

- URL 必须是 **https**,不能带用户名密码,不能是私网 / 回环 / 链路本地地址或 `localhost`。**真正的防护在连接时**:控制面解析域名后检查实际要连的 IP,拒绝私网、回环、链路本地、运营商 NAT(100.64/10)、多播、保留地址——所以指向内网的域名、DNS 重绑定都过不了;不跟随重定向。
- 每个项目最多 20 个。
- `GET /projects/<p>/webhooks` 列出(不含密钥;有 `failures`、`last_status`、`last_at`、`disabled`);`DELETE /projects/<p>/webhooks/<id>`;`POST …/<id>/test` 发一个 `ping`;`POST …/<id>/enable` 重新打开被自动关掉的。

## 投递

对你的地址 `POST`,`Content-Type: application/json`,头:

| 头 | 值 |
| --- | --- |
| `X-Cairn-Event` | 事件名 |
| `X-Cairn-Delivery` | 这次投递的 id(`dl-…`),重试时不变,可用来去重 |
| `X-Cairn-Signature` | `sha256=` + 十六进制的 `HMAC-SHA256(secret, 原始请求体)` |

请求体:`{"id": "dl-…", "type": "sandbox.created", "project": "acme", "created": 1767225600.5, "data": {…}}`。

**验签**(Python):
```python
import hmac, hashlib
def verify(secret: str, body: bytes, header: str) -> bool:
    return hmac.compare_digest(header, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
```
用**原始字节**验,不要先解析再序列化。

**可靠性**:2xx 算成功;否则立即重试,再过 5 秒、再过 30 秒各一次,共 3 次;全部失败记一次失败。**连续 10 次失败自动关闭**这个 webhook(列表里 `disabled: true`),修好后 `enable`。至多一次语义以外的保证**没有**:控制面重启、队列满(1024 条)、多副本时都可能丢事件——要对账用 [`GET /audit`](ops.md) 或列表接口,别把 webhook 当唯一来源。事件只来自本副本写下的审计行,每条审计行只会被写它的那个副本投递一次。
