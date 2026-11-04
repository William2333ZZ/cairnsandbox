#!/usr/bin/env python3
"""Collecting what other people have learned about agent harnesses, on a schedule (docs/007 采集与吸收).

Two sources, both without a model:
  papers  arXiv's daily "new submissions" listing pages (cs.SE, cs.AI, cs.MA, cs.CL, cs.LG): title and abstract, scored by words that matter to a coding-agent harness.
          (The arXiv API host is not reachable from the dev box; the listing pages and github.com are.)
  repos   open-source agent harnesses on GitHub: new commits that touch prompts, tool descriptions, middleware, context handling, memory or skills, and releases (changelogs).
Items go to <state>/items.jsonl once (a seen-set keeps them from coming back), and each run writes <state>/digest-YYYY-MM-DD.md, the top of the day by score.
What is done with them is a separate step (`ideas`: a model reads the best few and writes hypotheses we can test, docs/007); collecting costs nothing.

usage: harvest.py papers|repos|all [--state DIR] [--min-score N]      harvest.py loop [--every SECONDS] [--state DIR]"""
import argparse, json, os, re, sys, time, urllib.error, urllib.request

UA = "cairn-harvest/1 (research notes; contact: repo owner)"
LISTINGS = ["cs.SE", "cs.AI", "cs.MA", "cs.CL", "cs.LG"]
REPOS = ["SWE-agent/mini-swe-agent", "SWE-agent/SWE-agent", "All-Hands-AI/OpenHands", "Aider-AI/aider", "sst/opencode", "openai/codex", "cline/cline", "block/goose",
         "google-gemini/gemini-cli", "anthropics/claude-code", "laude-institute/terminal-bench", "stanford-iris-lab/meta-harness"]
PATH_RE = re.compile(r"(prompt|system[_-]?message|instruction|tool|middleware|hook|compact|summar|context|memory|skill|agent|loop|truncat|cache|condens|planner|subagent)", re.I)
SKIP_PATH_RE = re.compile(r"(^|/)(tests?|docs?|examples?|node_modules|vendor|dist|\.github)/|\.(md|lock|png|svg|json)$|package-lock", re.I)
# word -> weight; a paper or a commit about these is about the thing we are building
TERMS = {r"agent(ic)? harness": 5, r"\bharness\b": 3, r"scaffold": 3, r"coding agent": 3, r"software engineering agent": 3, r"swe-?bench": 2, r"terminal-?bench": 3, r"self-?improv": 3, r"self-?evol": 3,
         r"context (compaction|compression|management|engineering)": 4, r"prompt cach": 3, r"middleware": 3, r"\bskills?\b": 2, r"long-?term memory|agent memory": 3, r"tool (use|description|calling)": 2,
         r"trajector": 2, r"human-in-the-loop|ask(ing)? clarif|clarifying question": 3, r"user (correction|feedback|preference)": 3, r"regression (gate|test)": 2, r"sub-?agents?": 2, r"long-horizon": 2}


def score(text):
    """(score, matched terms) of a text against what a coding-agent harness cares about."""
    t = re.sub(r"[_/.]+", " ", (text or "").lower()); hits = {}                          # file names count: context_compaction.py is "context compaction"
    for pat, w in TERMS.items():
        if re.search(pat, t): hits[pat] = w
    return sum(hits.values()), sorted(hits)


def fetch(url, accept=None, retries=2, pause=3.0):
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, **({"Accept": accept} if accept else {})}), timeout=40) as r: return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < retries: time.sleep(pause * (i + 2)); continue
            raise
    return ""


def clean(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip().replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')


def parse_arxiv_listing(html):
    """Entries of the "New submissions" part of an arxiv.org/list/<cat>/new page (cross-lists are other categories' papers: they are skipped here and seen there)."""
    cut = html.find("Cross submissions"); html = html[:cut] if cut > 0 else html
    out = []
    for dt, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", html, flags=re.S):
        m = re.search(r'/abs/([0-9]{4}\.[0-9]{4,6})', dt)
        if not m: continue
        title = clean((re.search(r"list-title[^>]*>(.*?)</div>", dd, re.S) or [None, ""])[1]).replace("Title:", "").strip()
        abstract = clean((re.search(r"<p class=['\"]mathjax['\"]>(.*?)</p>", dd, re.S) or [None, ""])[1])
        subjects = clean((re.search(r"list-subjects[^>]*>(.*?)</div>", dd, re.S) or [None, ""])[1]).replace("Subjects:", "").strip()
        out.append({"id": "arxiv:" + m.group(1), "kind": "paper", "title": title, "text": abstract, "subjects": subjects, "url": "https://arxiv.org/abs/" + m.group(1)})
    return out


def papers(get=fetch, listings=LISTINGS, pause=3.0):
    seen, out = set(), []
    for cat in listings:
        try: html = get(f"https://arxiv.org/list/{cat}/new?skip=0&show=2000")
        except Exception as e: print(f"listing {cat} failed: {e}", file=sys.stderr); continue
        for it in parse_arxiv_listing(html):
            if it["id"] in seen: continue
            seen.add(it["id"]); it["score"], it["matched"] = score(it["title"] + " " + it["text"]); out.append(it)
        time.sleep(pause if get is fetch else 0)
    return out


def github_items(repo, since, get=fetch, token="", max_commits=15, max_detail=6):
    """Commits since an ISO time that touch harness-relevant files, and recent releases. `get` returns the JSON text of a GitHub API url."""
    h = lambda u: json.loads(get(u))      # noqa: E731
    out = []
    commits = h(f"https://api.github.com/repos/{repo}/commits?per_page={max_commits}" + (f"&since={since}" if since else ""))
    detail = 0
    for c in commits if isinstance(commits, list) else []:
        sha = c.get("sha", ""); msg = (c.get("commit") or {}).get("message", "")
        if len(c.get("parents") or []) > 1 or detail >= max_detail: continue                  # merges say nothing; the API budget is small
        try: files = (h(f"https://api.github.com/repos/{repo}/commits/{sha}").get("files") or []); detail += 1
        except Exception: continue                                                           # noqa: BLE001
        rel = [f for f in files if PATH_RE.search(f.get("filename", "")) and not SKIP_PATH_RE.search(f.get("filename", ""))]
        if not rel: continue
        text = msg + "\n" + "\n".join(f"{f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})" for f in rel[:12])
        s, matched = score(text); s += min(6, 2 * len(rel))
        out.append({"id": f"git:{repo}@{sha[:10]}", "kind": "commit", "repo": repo, "title": msg.splitlines()[0][:140] if msg else sha[:10], "text": text[:1500], "files": [f["filename"] for f in rel[:12]],
                    "url": c.get("html_url", ""), "date": ((c.get("commit") or {}).get("author") or {}).get("date", ""), "score": s, "matched": matched})
    try:
        for r in (h(f"https://api.github.com/repos/{repo}/releases?per_page=3") or []):
            body = r.get("body") or ""; s, matched = score(body + " " + (r.get("name") or ""))
            out.append({"id": f"rel:{repo}@{r.get('tag_name')}", "kind": "release", "repo": repo, "title": f"{repo} {r.get('name') or r.get('tag_name')}", "text": body[:2500], "url": r.get("html_url", ""),
                        "date": r.get("published_at", ""), "score": s + 1, "matched": matched})
    except Exception: pass                                                                   # noqa: BLE001 - a repo without releases
    return out


def repos(state, get=None, repo_list=REPOS):
    token = os.environ.get("GITHUB_TOKEN", "")
    def g(u):
        r = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/vnd.github+json", **({"Authorization": "Bearer " + token} if token else {})})
        with urllib.request.urlopen(r, timeout=40) as x: return x.read().decode("utf-8", "replace")
    get = get or g; out = []; last = state.setdefault("last", {})
    for repo in repo_list:
        since = last.get(repo) or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - 14 * 86400))
        try: out += github_items(repo, since, get=get)
        except Exception as e: print(f"repo {repo} failed: {e}", file=sys.stderr); continue
        last[repo] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return out


def load_state(d):
    try: return json.load(open(os.path.join(d, "harvest.json")))
    except (OSError, ValueError): return {"seen": [], "last": {}}


def save_state(d, st):
    os.makedirs(d, exist_ok=True); st["seen"] = st["seen"][-20000:]
    tmp = os.path.join(d, "harvest.json.tmp"); json.dump(st, open(tmp, "w")); os.replace(tmp, os.path.join(d, "harvest.json"))


def run(kind, state_dir, min_score=5, papers_get=fetch, repos_get=None, top=25):
    st = load_state(state_dir); seen = set(st["seen"]); items = []
    if kind in ("papers", "all"): items += papers(get=papers_get)
    if kind in ("repos", "all"): items += repos(st, get=repos_get)
    new = [i for i in items if i["id"] not in seen and i["score"] >= min_score]
    new.sort(key=lambda i: -i["score"])
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "items.jsonl"), "a") as f:
        for i in new: f.write(json.dumps({**i, "harvested": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, ensure_ascii=False) + "\n")
    st["seen"] = list(seen | {i["id"] for i in items})                                       # items below the bar are seen too: they are not re-scored every run
    save_state(state_dir, st)
    if new:
        lines = [f"# Harvest {time.strftime('%Y-%m-%d')}: {len(new)} new, best {top}\n"] + [f"- **{i['score']}** [{i['kind']}] [{i['title']}]({i['url']}) — {', '.join(i['matched'])[:90]}" for i in new[:top]]
        open(os.path.join(state_dir, f"digest-{time.strftime('%Y-%m-%d')}.md"), "a").write("\n".join(lines) + "\n\n")
    return new


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("what", choices=["papers", "repos", "all", "loop"]); ap.add_argument("--state", default=os.environ.get("HARVEST_STATE", "harvest-state"))
    ap.add_argument("--min-score", type=int, default=5); ap.add_argument("--every", type=int, default=21600)
    a = ap.parse_args()
    while True:
        try: new = run("all" if a.what == "loop" else a.what, a.state, a.min_score); print(time.strftime("%H:%M:%S"), f"{len(new)} new items", flush=True)
        except Exception as e: print("harvest failed:", repr(e), flush=True)                  # noqa: BLE001
        if a.what != "loop": break
        time.sleep(a.every)
