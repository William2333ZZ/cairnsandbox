#!/usr/bin/env python3
"""Model API proxy, outside the sandbox (D4: cost limits live where the agent cannot reach them).

OpenAI-compatible: POST /v1/chat/completions with `Authorization: Bearer <sandbox token>`. Each request is metered
per sandbox (calls, tokens in/out, USD from a price table) and refused with 402 once the sandbox budget is spent.
Response headers X-Cost-USD / X-Tokens-In / X-Tokens-Out let the agent report cost without being trusted for it.

Tokens: POST /tokens {sandbox, budget_usd?, budget_tokens?, model?} -> {token}  (caller: evaluator / control plane; needs
        PROXY_ADMIN_TOKEN when set).  GET /usage/<sandbox>  GET /usage  GET /health
Backends (MODEL_BACKEND): "upstream" = forward to UPSTREAM_URL (OpenAI-compatible, e.g. https://api.deepseek.com/v1)
        with UPSTREAM_KEY; "mock" = deterministic scripted tool calls (CI / pipeline tests, no network).
Prices: MODEL_PRICES='{"deepseek-chat":[0.28,0.42,0.0056]}' USD per million tokens [in (cache miss), out, in (cache hit)]; the third number is optional
(without it every input token costs the first). Cache hits come from the upstream usage (DeepSeek: prompt_cache_hit_tokens; OpenAI style: prompt_tokens_details.cached_tokens).
Unknown model -> 0 and flagged.
"""
import json, os, secrets, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PROXY_PORT", "8500")); STATE = os.environ.get("PROXY_STATE", "/var/lib/fc-model")
BACKEND = os.environ.get("MODEL_BACKEND", "mock"); UPSTREAM = os.environ.get("UPSTREAM_URL", "").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "") or (open(os.environ["UPSTREAM_KEY_FILE"]).read().strip() if os.environ.get("UPSTREAM_KEY_FILE") else "")   # file keeps the key out of `docker inspect`
ADMIN = os.environ.get("PROXY_ADMIN_TOKEN"); DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "deepseek-flash")
# USD per million tokens [in (cache miss), out, in (cache hit)]; DeepSeek V4.1 Flash = "deepseek-flash", peak-hour list price (off-peak is half) per api-docs.deepseek.com/quick_start/pricing.
# A cache hit costs 1/50 of a miss: an agent loop resends the same prefix every step, so charging all input at the miss price overstated its cost many times over.
PRICES = json.loads(os.environ.get("MODEL_PRICES") or '{"deepseek-flash":[0.30,1.20,0.006],"deepseek-chat":[0.30,1.20,0.006],"mock":[0,0,0]}')
# Accounts (D28): who pays for a call. A token names one ("live" for the agent people talk to, "eval" for evaluation and evolution, anything else for the default); each may have its own upstream key,
# so a balance spent on experiments cannot stop the agent a person is waiting on (the 402 of 2026-11-02 did, because both used one key).  UPSTREAM_KEY_FILES='{"eval": "/secrets/deepseek-eval.key"}'
KEY_FILES = json.loads(os.environ.get("UPSTREAM_KEY_FILES") or "{}")
MAX_BODY = 4 << 20
os.makedirs(STATE, exist_ok=True)
LOCK = threading.Lock()
TOKENS = {}     # token -> {"sandbox", "budget_usd", "budget_tokens", "model", "created"}
USAGE = {}      # sandbox -> {"calls","tokens_in","tokens_out","tokens_cached","cost_usd","refused","unpriced"}
LOG = open(f"{STATE}/usage.jsonl", "ab", buffering=0)


def log(**ev): ev["ts"] = round(time.time(), 3); LOG.write((json.dumps(ev) + "\n").encode())

def usage_of(sb): return USAGE.setdefault(sb, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_usd": 0.0, "refused": 0, "unpriced": 0})

def price(model, tin, tout, cached=0):
    """USD for one call. `cached` of the `tin` input tokens were served from the provider's prefix cache and cost the (much lower) hit price when the table has one."""
    p = PRICES.get(model)
    if not p: return None
    cached = max(0, min(int(cached or 0), int(tin)))
    hit = p[2] if len(p) > 2 else p[0]
    return ((tin - cached) * p[0] + cached * hit + tout * p[1]) / 1e6

def usage_of_response(body, resp):
    """(usage, tokens in, tokens out, tokens cached) of an upstream answer. A provider that leaves the usage out, or sends nulls, must not make a call free: then the size of the
    request and the answer, at three characters a token (an over-estimate), stands in, so a budget still binds."""
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    def num(k):
        try: return max(0, int(usage.get(k) or 0))
        except (TypeError, ValueError): return 0
    tin, tout = num("prompt_tokens"), num("completion_tokens")
    if not tin: tin = len(json.dumps(body.get("messages") or [], ensure_ascii=False)) // 3
    if not tout: tout = len(json.dumps(resp.get("choices") or [], ensure_ascii=False)) // 3
    return usage, tin, tout, cached_tokens(usage)


def cached_tokens(usage):
    """How many prompt tokens the provider says it served from cache; 0 when it does not say."""
    try:
        n = usage.get("prompt_cache_hit_tokens")
        if n is None: n = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        return max(0, int(n or 0))
    except (TypeError, ValueError, AttributeError): return 0


# ---------------- backends ----------------
def account_key(account):
    """The upstream key an account pays with: its own file when one is configured and readable, else the default key."""
    path = KEY_FILES.get(account or "")
    if path:
        try:
            k = open(path).read().strip()
            if k: return k
        except OSError: pass
    return UPSTREAM_KEY


def accounts_view():
    return {a: ("own key" if account_key(a) != UPSTREAM_KEY else "shared key") for a in sorted(set(KEY_FILES) | {"live", "eval"})}


def call_upstream(body, timeout=300, account=None):
    req = urllib.request.Request(UPSTREAM + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {account_key(account)}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except ValueError: return e.code, {"error": {"message": raw[:300].decode(errors="replace")}}


MOCK_SCRIPT = json.loads(os.environ.get("MOCK_SCRIPT") or '["ls -la", "cat .task/prompt.md 2>/dev/null | head -5", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]')

def call_mock(body):
    """Deterministic: the n-th assistant turn issues MOCK_SCRIPT[n]; after the script ends, submit.
    A user message containing 'MOCK:' followed by a JSON list overrides the script (lets tests drive the mock)."""
    msgs = body.get("messages", []); n = sum(1 for m in msgs if m.get("role") == "assistant")
    script = MOCK_SCRIPT
    for m in msgs:
        c = m.get("content") if isinstance(m.get("content"), str) else ""
        if m.get("role") in ("user", "system") and "MOCK:" in c:
            try: script = json.loads(c.split("MOCK:", 1)[1].strip().splitlines()[0])
            except ValueError: pass
    cmd = script[n] if n < len(script) else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    tin = sum(len(json.dumps(m)) for m in msgs) // 4; tout = len(cmd) // 4 + 8
    return 200, {"id": f"mock-{n}", "object": "chat.completion", "model": "mock", "choices": [{"index": 0, "finish_reason": "tool_calls",
                 "message": {"role": "assistant", "content": f"step {n}", "tool_calls": [{"id": f"call_{n}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]}}],
                 "usage": {"prompt_tokens": tin, "completion_tokens": tout, "total_tokens": tin + tout}}


# ---------------- http ----------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj, headers=None):
        data = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items(): self.send_header(k, str(v))
        self.end_headers(); self.wfile.write(data)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY: raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")
    def _bearer(self):
        a = self.headers.get("Authorization", ""); return a[7:].strip() if a.startswith("Bearer ") else ""

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health": return self._send(200, {"ok": True, "backend": BACKEND, "upstream": bool(UPSTREAM), "tokens": len(TOKENS), "sandboxes": len(USAGE), "prices": sorted(PRICES), "accounts": accounts_view()})
        if p == "/usage":
            if ADMIN and self._bearer() != ADMIN: return self._send(401, {"error": "admin token"})
            return self._send(200, {"usage": USAGE})
        if p.startswith("/usage/"):
            if ADMIN and self._bearer() != ADMIN: return self._send(401, {"error": "admin token"})
            return self._send(200, {"sandbox": p[7:], **usage_of(p[7:])})
        if p == "/v1/models": return self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in PRICES]})
        self._send(404, {"error": "no route"})

    def do_DELETE(self):
        if self.path.startswith("/tokens/"):
            if ADMIN and self._bearer() != ADMIN: return self._send(401, {"error": "admin token"})
            with LOCK: t = TOKENS.pop(self.path[8:], None)
            return self._send(200 if t else 404, {"revoked": bool(t)})
        self._send(404, {"error": "no route"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try: body = self._body()
        except ValueError as e: return self._send(400, {"error": str(e)})
        if p == "/tokens":
            if ADMIN and self._bearer() != ADMIN: return self._send(401, {"error": "admin token"})
            sb = body.get("sandbox")
            if not sb: return self._send(400, {"error": "sandbox required"})
            tok = secrets.token_urlsafe(24)
            with LOCK: TOKENS[tok] = {"sandbox": sb, "budget_usd": float(body.get("budget_usd", 1.0)), "budget_tokens": int(body.get("budget_tokens", 0)), "model": body.get("model"), "account": str(body.get("account") or "live")[:20], "created": time.time()}
            log(op="token", sandbox=sb, account=TOKENS[tok]["account"], budget_usd=TOKENS[tok]["budget_usd"]); return self._send(201, {"token": tok, "sandbox": sb})
        if p == "/v1/chat/completions":
            t = TOKENS.get(self._bearer())
            if not t: return self._send(401, {"error": {"message": "unknown sandbox token"}})
            sb = t["sandbox"]; u = usage_of(sb); model = "mock" if BACKEND == "mock" else (t["model"] or body.get("model") or DEFAULT_MODEL)
            with LOCK:
                over = (t["budget_usd"] and u["cost_usd"] >= t["budget_usd"]) or (t["budget_tokens"] and u["tokens_in"] + u["tokens_out"] >= t["budget_tokens"])
                if over: u["refused"] += 1
            if over: log(op="refused", sandbox=sb, cost=u["cost_usd"]); return self._send(402, {"error": {"message": f"budget exhausted for {sb}", "usage": u}})
            if model not in PRICES: return self._send(400, {"error": {"message": f"model {str(model)[:60]!r} has no price here; a call that cannot be priced cannot be held to a budget"}})
            body["model"] = model; body.pop("stream", None); t0 = time.monotonic()
            if BACKEND == "mock": st, resp = call_mock(body)
            elif UPSTREAM: st, resp = call_upstream(body, account=t.get("account"))
            else: return self._send(503, {"error": {"message": "no upstream configured"}})
            if st != 200: log(op="upstream_error", sandbox=sb, status=st); return self._send(st, resp)
            usage, tin, tout, tcached = usage_of_response(body, resp); cost = price(model, tin, tout, tcached)
            with LOCK:
                u["calls"] += 1; u["tokens_in"] += tin; u["tokens_out"] += tout; u["tokens_cached"] += min(tcached, tin)
                if cost is None: u["unpriced"] += 1; cost = 0.0
                u["cost_usd"] = round(u["cost_usd"] + cost, 6)
            log(op="call", sandbox=sb, account=t.get("account"), model=model, tokens_in=tin, tokens_out=tout, tokens_cached=min(tcached, tin), cost=round(cost, 6), s=round(time.monotonic() - t0, 3))
            return self._send(200, resp, {"X-Cost-USD": f"{cost:.6f}", "X-Tokens-In": tin, "X-Tokens-Out": tout, "X-Tokens-Cached": min(tcached, tin), "X-Sandbox": sb})
        self._send(404, {"error": "no route"})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H); srv.daemon_threads = True
    print(f"fc-model: backend={BACKEND} upstream={'set' if UPSTREAM else 'none'} port={PORT} state={STATE}", flush=True); srv.serve_forever()

if __name__ == "__main__": main()
