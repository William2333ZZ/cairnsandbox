# GitHub as a Connector (docs/002): the same read and write ends as Gitea, over the GitHub REST API. The token lives only here;
# sandboxes never see it. Everything the engine calls on a code host is the method set of Gitea, so this subclasses it and
# only changes the wire details.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *
from gitea import Gitea


class GitHub(Gitea):
    kind = "github"

    def __init__(self, cfg):
        self.url, self.api_url = cfg.get("github_url") or "https://github.com", (cfg.get("github_api") or "https://api.github.com").rstrip("/")
        self.repo, self.bot, self.base = cfg.get("github_repo") or "", cfg.get("github_bot") or "", cfg["base_branch"]
        self.token = ""
        f = cfg.get("github_token_file") or ""
        if f and os.path.exists(f): self.token = open(f).read().strip().split()[-1]
        self.enabled = bool(self.repo and self.token); self.readable = bool(self.repo); self.me = None

    def _headers(self, json_body=False):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fc-node-agent"}
        if self.token: h["Authorization"] = "Bearer " + self.token
        if json_body: h["Content-Type"] = "application/json"
        return h

    def api(self, method, path, body=None, timeout=30):
        req = urllib.request.Request(self.api_url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=self._headers(body is not None))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = r.read(); return json.loads(d) if d else {}

    def _bytes(self, path, timeout=120, accept=None):
        req = urllib.request.Request(self.api_url + path, headers={**self._headers(), **({"Accept": accept} if accept else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()

    def comments_since(self, since_iso):
        return self.api("GET", f"/repos/{self.repo}/issues/comments?since={urllib.parse.quote(since_iso)}&sort=updated&direction=asc&per_page=50")

    def issues_since(self, since_iso):                                                   # GitHub lists pull requests as issues too; the source tells them apart by `pull_request`
        return self.api("GET", f"/repos/{self.repo}/issues?state=all&since={urllib.parse.quote(since_iso)}&per_page=50")

    def pull(self, n):
        p = self.api("GET", f"/repos/{self.repo}/pulls/{n}")
        if p and "merge_base" not in p: p["merge_base"] = (p.get("base") or {}).get("sha")        # GitHub has no merge_base field; the base sha is what the branch was cut from
        return p

    def pr_files(self, n):
        out = []
        for page in range(1, 31):
            rows = self.api("GET", f"/repos/{self.repo}/pulls/{n}/files?per_page=100&page={page}") or []
            out += rows
            if len(rows) < 100: break
        return out
    def raw(self, path, ref): return self._bytes(f"/repos/{self.repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}", accept="application/vnd.github.raw+json")
    def archive(self, sha): return self._bytes(f"/repos/{self.repo}/tarball/{sha}", timeout=300)
    def pr_diff(self, n): return self._bytes(f"/repos/{self.repo}/pulls/{n}", accept="application/vnd.github.diff")
    def merge_pr(self, n, head=None): return self.api("PUT", f"/repos/{self.repo}/pulls/{n}/merge", {"merge_method": "merge", **({"sha": head} if head else {})})
    def open_pr(self, head, title, body): return self.api("POST", f"/repos/{self.repo}/pulls", {"head": head, "base": self.base, "title": title, "body": body})

    def clone_url(self):
        if not self.token: return f"{self.url}/{self.repo}.git"
        return f"https://x-access-token:{self.token}@{urllib.parse.urlsplit(self.url).netloc}/{self.repo}.git"
