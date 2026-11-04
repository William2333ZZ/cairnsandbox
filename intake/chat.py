# A chat tool as a Connector (docs/001 第 2 阶段, docs/002 Connector): messages come in through a webhook the tool calls, the
# agent's words go out through the tool's incoming-webhook URL. Formats: generic JSON, Feishu (飞书 custom bot + event
# subscription), Slack (incoming webhook + Events API). The webhook secret and the outgoing URL live only here.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *


class ChatConnector:
    kind = "chat"

    def __init__(self, cfg):
        self.format = (cfg.get("chat_format") or "generic").lower()
        self.webhook = cfg.get("chat_webhook_url") or ""                                   # outgoing: where the agent's words go
        self.secret = ""
        f = cfg.get("chat_secret_file") or ""
        if f and os.path.exists(f): self.secret = open(f).read().strip()
        self.bot = cfg.get("chat_bot") or "cairn"
        self.enabled = bool(self.webhook); self.readable = bool(self.secret)              # write side needs the URL; read side needs the shared secret
        self.url = self.webhook.split("?")[0][:60] if self.webhook else ""

    # ---- outgoing
    def payload(self, text):
        if self.format == "feishu": return {"msg_type": "text", "content": {"text": text}}
        if self.format == "slack": return {"text": text}
        return {"text": text, "from": self.bot}

    def send(self, text, thread=None):
        if not self.enabled: raise RuntimeError("chat webhook not configured")
        body = self.payload(text)
        if thread and self.format == "generic": body["thread"] = thread
        req = urllib.request.Request(self.webhook, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r: return r.read()[:200]

    # ---- incoming
    def verify(self, headers, body):
        """The tool proves itself with the shared secret: generic/Slack in a header, Feishu in the event body's token."""
        if not self.secret: return False
        given = headers.get("X-Chat-Secret") or (body.get("token") if isinstance(body, dict) else None) or ((body.get("header") or {}).get("token") if isinstance(body, dict) else None) or ""
        return hmac.compare_digest(str(given), self.secret)

    def parse(self, body):
        """→ ({actor, text, thread, dm}) or ({challenge}) for a URL-verification handshake, or None when there is nothing to hear."""
        if not isinstance(body, dict): return None
        if body.get("type") == "url_verification" or ("challenge" in body and "event" not in body): return {"challenge": body.get("challenge")}
        if self.format == "feishu" or body.get("schema") == "2.0":
            ev = body.get("event") or {}; msg = ev.get("message") or {}
            if (body.get("header") or {}).get("event_type", "im.message.receive_v1") != "im.message.receive_v1": return None
            try: text = json.loads(msg.get("content") or "{}").get("text", "")
            except ValueError: text = msg.get("content") or ""
            sender = ((ev.get("sender") or {}).get("sender_id") or {}); actor = sender.get("open_id") or sender.get("user_id") or "unknown"
            return {"actor": actor, "text": re.sub(r"@_user_\d+\s*", "", text).strip(), "thread": msg.get("chat_id") or actor, "dm": msg.get("chat_type") == "p2p", "id": msg.get("message_id")}
        if self.format == "slack" or "event" in body and (body.get("event") or {}).get("type") == "message":
            ev = body.get("event") or {}
            if ev.get("bot_id") or ev.get("subtype"): return None
            return {"actor": ev.get("user") or "unknown", "text": (ev.get("text") or "").strip(), "thread": ev.get("channel") or ev.get("user"), "dm": ev.get("channel_type") == "im", "id": ev.get("ts")}
        if body.get("text"): return {"actor": str(body.get("actor") or body.get("user") or "unknown"), "text": str(body["text"]).strip(), "thread": str(body.get("thread") or body.get("actor") or "dm"), "dm": bool(body.get("dm", True)), "id": body.get("id")}
        return None

    def trigger(self, m):
        """A chat message as a Trigger: a direct message addresses the agent; in a group only an @mention does."""
        text = m["text"]; addressed = m.get("dm", True) or f"@{self.bot}" in text
        tid = "chat-" + (str(m.get("id")) if m.get("id") else hashlib.sha256(f"{m['actor']}|{m['thread']}|{text}|{int(now() // 5)}".encode()).hexdigest()[:12])
        return {"id": tid, "source": "chat", "actor": m["actor"], "human": True, "addressed": addressed, "scope": "dm" if m.get("dm", True) else "group", "text": text, "url": None,
                "threads": [f"chat:{m['thread']}"], "channel": {"kind": "chat", "thread": m["thread"]}, "meta": {"title": text[:80], "dm": m.get("dm", True)}}
