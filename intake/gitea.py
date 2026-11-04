# Gitea: the code host as a Connector — the read end (GiteaSource: issues, comments) and the write end (comments, PRs, merge) (docs/002 Connector).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *
from clients import InfraError

# ----------------------------------------------------------------------------------------------- gitea (source + relay)

class Gitea:
    """Both the Source's read side (polled) and the relay's write side. The token lives only here. Every code host Connector
    has this method set (GitHub subclasses it); `kind` names the Source, the channel kind and the policy section."""
    kind = "gitea"

    def __init__(self, cfg):
        self.url, self.repo, self.bot, self.base = cfg["gitea_url"], cfg["gitea_repo"], cfg["gitea_bot"], cfg["base_branch"]
        self.token = ""
        if cfg["gitea_token_file"] and os.path.exists(cfg["gitea_token_file"]):
            self.token = open(cfg["gitea_token_file"]).read().strip().split()[-1]
        self.enabled = bool(self.url and self.repo and self.token)      # write side (relay, polling as the bot)
        self.readable = bool(self.url and self.repo)                      # read side: a public repository clones without the token
        self.me = None

    def api(self, method, path, body=None, timeout=30):
        req = urllib.request.Request(self.url + "/api/v1" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": "token " + self.token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = r.read(); return json.loads(d) if d else {}

    def whoami(self):
        if self.me is None: self.me = self.api("GET", "/user").get("login")
        return self.me

    def comments_since(self, since_iso):
        q = f"/repos/{self.repo}/issues/comments?since={urllib.parse.quote(since_iso)}&limit=50"
        return self.api("GET", q)

    def issues_since(self, since_iso):
        return self.api("GET", f"/repos/{self.repo}/issues?state=all&type=issues&since={urllib.parse.quote(since_iso)}&limit=50")

    def issue(self, n): return self.api("GET", f"/repos/{self.repo}/issues/{n}")
    def pull(self, n): return self.api("GET", f"/repos/{self.repo}/pulls/{n}")
    def pr_files(self, n):
        out = []
        for page in range(1, 11):                                                          # the server caps a page (50 on Gitea): a long PR is read page by page, or its last files escape the review
            rows = self.api("GET", f"/repos/{self.repo}/pulls/{n}/files?limit=50&page={page}") or []
            out += rows
            if len(rows) < 50: break
        return out
    def _bytes(self, path, timeout=120):
        req = urllib.request.Request(self.url + "/api/v1" + path, headers={"Authorization": "token " + self.token})
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()
    def raw(self, path, ref): return self._bytes(f"/repos/{self.repo}/raw/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}")
    def archive(self, sha): return self._bytes(f"/repos/{self.repo}/archive/{sha}.tar.gz", timeout=300)      # the repository at one commit, no .git
    def pr_diff(self, n): return self._bytes(f"/repos/{self.repo}/pulls/{n}.diff")
    def merge_pr(self, n, head=None): return self.api("POST", f"/repos/{self.repo}/pulls/{n}/merge", {"Do": "merge", **({"head_commit_id": head} if head else {})})          # only after an approved merge Approval (docs/009)
    def comment(self, n, text): return self.api("POST", f"/repos/{self.repo}/issues/{n}/comments", {"body": text})

    def open_pr(self, head, title, body):
        return self.api("POST", f"/repos/{self.repo}/pulls", {"head": head, "base": self.base, "title": title, "body": body})

    def clone_url(self):
        u = urllib.parse.urlsplit(self.url)
        if not self.token: return f"{u.scheme}://{u.netloc}{u.path}/{self.repo}.git"
        return f"{u.scheme}://{self.bot}:{self.token}@{u.netloc}{u.path}/{self.repo}.git"

    def clone_tar(self, depth=1):
        """A shallow clone as a tar (bytes) for the sandbox: no token inside (the remote is rewritten to a dummy URL).
        Blobs over 1 MiB are left out when the server allows filtering (a layer binary that once sat in history must not
        travel into every sandbox); depth 1 by default."""
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "repo")
            r = subprocess.run(["git", "clone", "-q", f"--depth={depth}", "--filter=blob:limit=1m", "-b", self.base, self.clone_url(), d], capture_output=True, timeout=300)
            if r.returncode != 0:
                shutil.rmtree(d, ignore_errors=True)
                subprocess.run(["git", "clone", "-q", f"--depth={depth}", "-b", self.base, self.clone_url(), d], check=True, capture_output=True, timeout=300)
            subprocess.run(["git", "-C", d, "remote", "set-url", "origin", f"http://gitea.invalid/{self.repo}.git"], check=True, timeout=20)
            base_commit = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=20).stdout.strip()
            tar = subprocess.run(["tar", "-C", tmp, "-cf", "-", "repo"], capture_output=True, check=True, timeout=300).stdout
            return tar, base_commit

    def push_patch(self, branch, patch_bytes, base_commit):
        """Fresh clone, branch from base_commit, git am the sandbox's patches, push. Returns head sha."""
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "repo")
            subprocess.run(["git", "clone", "-q", "--depth=100", "-b", self.base, self.clone_url(), d], check=True, capture_output=True, timeout=300)
            subprocess.run(["git", "-C", d, "config", "user.name", self.bot], check=True); subprocess.run(["git", "-C", d, "config", "user.email", f"{self.bot}@noreply.local"], check=True)
            start = base_commit if subprocess.run(["git", "-C", d, "cat-file", "-e", base_commit], capture_output=True).returncode == 0 else "HEAD"
            subprocess.run(["git", "-C", d, "checkout", "-q", "-b", branch, start], check=True, capture_output=True, timeout=20)
            p = os.path.join(tmp, "t.patch"); open(p, "wb").write(patch_bytes)
            r = subprocess.run(["git", "-C", d, "am", "--committer-date-is-author-date", p], capture_output=True, text=True, timeout=120)
            if r.returncode != 0: raise RuntimeError(f"git am failed: {r.stderr[-400:]}")
            subprocess.run(["git", "-C", d, "push", "-q", "-f", "origin", branch], check=True, capture_output=True, timeout=300)
            return subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


class GiteaSource:
    """Polls issue/PR comments and new issues of the code host Connector (Gitea or GitHub); translates them to Triggers. Cursor in kv."""
    def __init__(self, engine):
        self.e = engine; self.g = engine.gitea; self._stop = threading.Event(); self.kind = getattr(self.g, "kind", "gitea")

    def paused(self): return bool(self.e.store.kv_get(f"connector.{self.kind}.paused", False))

    def _iso(self, t): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    def poll_once(self):
        g = self.g; k = self.kind
        if self.paused(): return 0
        cur = self.e.store.kv_get(f"{k}.cursor") or {"t": now() - 60}
        since = self._iso(cur["t"] - 5)
        bot = g.whoami() or g.bot
        newest = cur["t"]; n = 0
        for c in g.comments_since(since):
            created = _parse_iso(c.get("created_at")); newest = max(newest, created)
            user = (c.get("user") or {}).get("login", "")
            if user == bot: continue
            issue_url = c.get("issue_url") or ""; m = re.search(r"/issues/(\d+)$", issue_url)
            if not m: continue
            num = int(m.group(1))
            is_pr = bool(c.get("pull_request_url")) or "/pull/" in (c.get("html_url") or "")
            trig = {"id": f"{k}-c-{c['id']}", "source": k, "actor": user, "human": not BOT_RE.search(user), "addressed": f"@{bot}" in (c.get("body") or ""),
                    "scope": "pr_comment" if is_pr else "issue_comment", "text": c.get("body") or "", "url": c.get("html_url"), "threads": [f"{k}:{g.repo}#{num}"],
                    "channel": {"kind": k, "issue": num}, "meta": {"issue": num, "title": f"#{num}"}}
            self.e.receive(trig); n += 1
        for i in g.issues_since(since):
            created = _parse_iso(i.get("created_at")); newest = max(newest, created)
            if created < cur["t"] - 5: continue
            user = (i.get("user") or {}).get("login", "")
            if user == bot: continue
            num = i["number"]; is_pr = bool(i.get("pull_request"))
            text = (i.get("title") or "") + "\n\n" + (i.get("body") or "")
            trig = {"id": f"{k}-i-{i['id']}", "source": k, "actor": user, "human": not BOT_RE.search(user), "addressed": f"@{bot}" in text,
                    "scope": "new_pr" if is_pr else "new_issue", "text": text, "url": i.get("html_url"), "threads": [f"{k}:{g.repo}#{num}"],
                    "channel": {"kind": k, "issue": num}, "meta": {"issue": num, "title": f"#{num} {i.get('title', '')[:60]}"}}
            self.e.receive(trig); n += 1
        self.e.store.kv_set(f"{k}.cursor", {"t": max(newest, cur["t"]), "polled": now(), "last_n": n})
        self.e.problems.pop(k, None)
        return n

    def start(self):
        if not self.g.enabled: return
        def loop():
            fails = 0
            while not self._stop.is_set():
                try: self.poll_once(); fails = 0
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    if fails >= 3: self.e.problems[self.kind] = {"error": f"{type(e).__name__}: {e}"[:300], "since": now()}
                self._stop.wait(self.e.cfg["gitea_poll_s"])
        threading.Thread(target=loop, daemon=True).start()


def _parse_iso(s):
    if not s: return 0.0
    try:
        s = s.replace("Z", "+00:00")
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError: return 0.0
