# Configuration, constants and small helpers shared by every module of the Agent service (docs/005).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_STATES = ("progress", "waiting", "ready", "blocked", "failed", "dropped")
DECISIONS = ("accept", "stop", "ignore", "unavailable")
DEFAULT_AGENT_CMD = "/aios/bin/aios run --ticket --config /aios/harness/aios/config.json --out {out} --cwd {cwd}"
ASSIGNMENT_TIMEOUT_S = 1800
PASS_EVERY_S = 2.0
AGENT_LINE = "This is by an automated agent (aios), not a human."
BOT_RE = re.compile(r"(\[bot\]$|^fc-runner$|-bot$)", re.I)
STOP_RE = re.compile(r"^\s*(@[\w.-]+\s*)?(please\s+)?(stop|停止|停)(\s*(it|this|now|please|吧|一下|这个任务|这件事|这个))*\s*[.!。!]*\s*$", re.I)      # a person's stop, with or without addressing the bot
ROLLBACK_RE = re.compile(r"^\s*(@[\w.-]+\s*)?(退回|回退|回滚|roll\s*back|revert)(上一版|上一个版本|the last update)?\s*[。.!]?\s*$", re.I)   # 001 第 10 阶段: "退回" pins the previous version
CORRECTION_RE = re.compile(r"(不对|不是这|不是我|不要|别再|别用|别改|别动|别加|错了|不行|重做|重新来|改成|应该是|应该用|应该先|换成|撤销|多余|没必要|不用这么|wrong|don'?t|do not|shouldn'?t|should not|instead of|not what i|revert that|undo|redo|actually,)", re.I)   # 001 第 6 阶段: words that set the agent right
DISMISS_RE = re.compile(r"^\s*(随便|你(自己)?(定|决定|看着办|判断)|按你的|都行|都可以|不用问|不必问|whatever|your call|up to you|you decide|either|no preference)", re.I)   # an answer that says the question did not need asking
PROTECTED_PATHS = (".gitea/", "ci/", "run-node.sh", "node/Dockerfile")             # the relay never pushes patches that touch what CI runs as


def cfg_from_env():
    return {
        "port": int(os.environ.get("INTAKE_PORT", "8400")),
        "state": os.environ.get("INTAKE_STATE", os.path.join(HERE, "state")),
        "cp_url": os.environ.get("CP_URL", "http://127.0.0.1:18700").rstrip("/"),
        "cp_token": os.environ.get("CP_TOKEN") or (open(os.environ["CP_TOKEN_FILE"]).read().strip() if os.environ.get("CP_TOKEN_FILE") and os.path.exists(os.environ["CP_TOKEN_FILE"]) else ""),   # a compose / k8s secret file works too
        "model_proxy": os.environ.get("MODEL_PROXY_URL", "").rstrip("/"),
        "model_name": os.environ.get("CAIRN_MODEL_NAME", "deepseek-flash"),
        "template": os.environ.get("INTAKE_TEMPLATE", "debian-13"),
        "layers": [l for l in os.environ.get("INTAKE_LAYERS", "").split(",") if l],      # langs + harness + env layers of the default version
        "landlock": os.environ.get("INTAKE_LANDLOCK", "1") == "1",
        "max_parallel": max(1, min(4, int(os.environ.get("MAX_PARALLEL", "2")))),
        "assignment_timeout_s": int(os.environ.get("ASSIGNMENT_TIMEOUT_S", str(ASSIGNMENT_TIMEOUT_S))),
        "merge_s": float(os.environ.get("INTAKE_MERGE_S", "10")),
        "user": os.environ.get("INTAKE_USER", "default"),
        "project": os.environ.get("INTAKE_PROJECT", ""),                               # the control-plane Project this service's sandboxes belong to (D33 补; empty = the token's default)
        "eval_url": os.environ.get("EVAL_URL", "").rstrip("/"),                          # where mined tasks go (docs/007); empty = no mining                               # whose Agent this service runs (docs/009): one per deployment today                       # Router: a person's consecutive messages within this window become one Eval batch
        "gitea_url": os.environ.get("GITEA_URL", "").rstrip("/"),
        "gitea_repo": os.environ.get("GITEA_REPO", ""),                                      # owner/name
        "gitea_token_file": os.environ.get("GITEA_TOKEN_FILE", ""),
        "gitea_bot": os.environ.get("GITEA_BOT", "cairn-bot"),
        "gitea_poll_s": float(os.environ.get("GITEA_POLL_S", "20")),
        "code_host": os.environ.get("CODE_HOST", "gitea").lower(),
        "chat_format": os.environ.get("CHAT_FORMAT", "generic"), "chat_webhook_url": os.environ.get("CHAT_WEBHOOK_URL", ""),      # docs/002 Connector: a chat tool
        "chat_secret_file": os.environ.get("CHAT_SECRET_FILE", ""), "chat_bot": os.environ.get("CHAT_BOT", "cairn"),                          # gitea | github (docs/002 Connector)
        "github_repo": os.environ.get("GITHUB_REPO", ""), "github_token_file": os.environ.get("GITHUB_TOKEN_FILE", ""), "github_bot": os.environ.get("GITHUB_BOT", ""),
        "github_url": os.environ.get("GITHUB_URL", "https://github.com"), "github_api": os.environ.get("GITHUB_API", "https://api.github.com"),
        "base_branch": os.environ.get("BASE_BRANCH", "main"),
        "disk_mib": int(os.environ.get("INTAKE_DISK_MIB", "2048")),
        "clone_depth": int(os.environ.get("REPO_CLONE_DEPTH", "1")),       # what goes into the sandbox: a 200 MB history does not fit a 512 MiB guest's write path
        "home_volume": os.environ.get("INTAKE_HOME_VOLUME", "0") == "1",     # the harness directory rides as a read-only volume (one per digest) instead of a tar; costs a cold boot (D35)
    }


def now(): return time.time()
def new_id(prefix): return f"{prefix}-{uuid.uuid4().hex[:10]}"
def short_name(tid): return tid.split("-", 1)[1][:8]
def jdump(o): return json.dumps(o, sort_keys=True, separators=(",", ":"))

def unverified(actor, body):
    """Words from a source the person has not vouched for, fenced so the harness reads them as facts about the task. A closing tag inside the body cannot close the fence early."""
    return "<unverified from=\"" + re.sub(r"[^\w.@:/-]", "_", str(actor or "?"))[:60] + "\">\n" + str(body).replace("</unverified", "<\\/unverified") + "\n</unverified>"


BILLING_RE = re.compile(r"insufficient[ _]balance|payment required|account.{0,20}balance|quota (exceeded|exhausted)", re.I)      # the provider, not the job, is why it stopped
APPROVAL_KINDS = ("merge", "send", "budget", "routine")
ROUTINE_RE = re.compile(r"(以后|之后|从今天起|从明天起)?\s*每\s*(天|日|周|小时|(\d+)\s*(分钟|小时|天))|every\s+(day|hour|\d+\s*(minutes?|hours?|days?))|daily\b", re.I)
TIME_RE = re.compile(r"(早上|上午|中午|下午|晚上|凌晨)?\s*(\d{1,2})\s*[点:：]\s*(\d{2})?|(\d{1,2}):(\d{2})\s*(am|pm)?", re.I)


def parse_routine(text):
    """docs/005 例行: "以后每天早上八点,把昨晚的 CI 失败整理给我" → {text, daily_at} / {text, every_s}; None when the message is not recurring work."""
    m = ROUTINE_RE.search(text or "")
    if not m: return None
    unit, n, nunit = m.group(2), m.group(3), m.group(4)
    sc = {"text": text.strip()[:500], "enabled": True}
    if n and nunit: sc["every_s"] = int(n) * {"分钟": 60, "小时": 3600, "天": 86400}[nunit]
    elif unit in ("小时",) or (m.group(5) or "").lower() == "hour": sc["every_s"] = 3600
    elif m.group(5) and re.match(r"\d+", m.group(5)):
        q = re.match(r"(\d+)\s*(minute|hour|day)", m.group(5), re.I); sc["every_s"] = int(q.group(1)) * {"minute": 60, "hour": 3600, "day": 86400}[q.group(2).lower()]
    elif unit == "周": return None                                                        # weekly is not supported yet: let it be a normal request
    else:
        t = TIME_RE.search(text); hh, mm = 8, 0
        if t and t.group(2): hh = int(t.group(2)); mm = int(t.group(3) or 0); hh += 12 if (t.group(1) in ("下午", "晚上") and hh < 12) else 0
        elif t and t.group(4): hh = int(t.group(4)); mm = int(t.group(5)); hh += 12 if (t.group(6) or "").lower() == "pm" and hh < 12 else 0
        sc["daily_at"] = f"{hh % 24:02d}:{mm % 60:02d}"
    return sc                               # docs/009 批准: what a sandbox may only draft
SAY_KINDS = ("ack", "ready", "stuck", "question", "approval", "digest", "update")     # docs/006 对用户说话的规矩: the only ways the agent speaks
SAY_DELIVER = {"ack", "ready", "stuck", "question", "approval"}                        # sent back to where the message came from; digest/update stay in the conversation
