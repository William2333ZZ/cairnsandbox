#!/usr/bin/env python3
"""Import the regression tasks a person keeps in their harness directory (docs/004 个人 harness 目录: tasks/<repo>/<task-id>/ in CONTRACT §6 layout)
into the evaluator as taskset home-<user>-<repo>, one check batch per task (reference must pass, no-op must fail). The runtime never reads tasks/;
this is the evaluator's road into it (D29 的下一步).

usage: home-tasks.py --home DIR --eval http://127.0.0.1:18600 [--user NAME] [--repo NAME] [--token FILE] [--dry-run]
A task directory: prompt.md, init/ (the workspace), verify/*.py (hidden tests; any .py under verify/), optional reference/ (files laid over init that must pass),
optional meta.json {kind, rules, blockers, timeout_s, group}."""
import argparse, base64, io, json, os, re, sys, tarfile, urllib.request


def tar_gz(path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for f in sorted(files):
                p = os.path.join(root, f); tf.add(p, arcname=os.path.relpath(p, path))
    return buf.getvalue()


def files_under(path, suffix=None):
    out = {}
    for root, _, files in os.walk(path):
        for f in files:
            if suffix and not f.endswith(suffix): continue
            p = os.path.join(root, f); out[os.path.relpath(p, path)] = open(p, encoding="utf-8", errors="replace").read()
    return out


def task_body(tdir, tid):
    if not os.path.isfile(f"{tdir}/prompt.md") or not os.path.isdir(f"{tdir}/init"): return None, "needs prompt.md and init/"
    tests = files_under(f"{tdir}/verify", ".py") if os.path.isdir(f"{tdir}/verify") else {}
    if not tests: return None, "needs at least one .py test under verify/"
    meta = {}
    if os.path.isfile(f"{tdir}/meta.json"):
        try: meta = json.load(open(f"{tdir}/meta.json"))
        except ValueError as e: return None, f"meta.json: {e}"
    body = {"id": tid, "prompt": open(f"{tdir}/prompt.md", encoding="utf-8").read(), "init_tar_gz_b64": base64.b64encode(tar_gz(f"{tdir}/init")).decode(),
            "verify_files": tests, "reference_files": files_under(f"{tdir}/reference") if os.path.isdir(f"{tdir}/reference") else {},
            "source": {"origin": "home", "task_dir": os.path.relpath(tdir)}}
    for k in ("kind", "rules", "blockers", "timeout_s", "group"):
        if k in meta: body[k] = meta[k]
    return body, None


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--home", required=True); ap.add_argument("--eval", default=os.environ.get("EVAL_URL", "http://127.0.0.1:18600"))
    ap.add_argument("--user", default=os.environ.get("USER", "user")); ap.add_argument("--repo", default=None, help="only this repository's tasks"); ap.add_argument("--token", default=os.environ.get("CP_TOKEN_FILE"))
    ap.add_argument("--dry-run", action="store_true"); a = ap.parse_args(argv)
    root = os.path.join(a.home, "tasks")
    if not os.path.isdir(root): print(f"no tasks/ under {a.home}", file=sys.stderr); return 2
    tok = open(a.token).read().strip() if a.token else os.environ.get("CP_TOKEN", "")
    n_ok = n_bad = 0
    for repo in sorted(os.listdir(root)):
        if a.repo and repo != a.repo: continue
        rdir = os.path.join(root, repo)
        if not os.path.isdir(rdir): continue
        name = re.sub(r"[^A-Za-z0-9._-]", "-", f"home-{a.user}-{repo}")
        for tid in sorted(os.listdir(rdir)):
            tdir = os.path.join(rdir, tid)
            if not os.path.isdir(tdir): continue
            body, err = task_body(tdir, re.sub(r"[^A-Za-z0-9._-]", "-", tid))
            if err: print(f"skip {repo}/{tid}: {err}"); n_bad += 1; continue
            if a.dry_run: print(f"would import {repo}/{tid} -> {name} ({len(body['verify_files'])} tests, {len(body['reference_files'])} reference files)"); n_ok += 1; continue
            req = urllib.request.Request(f"{a.eval.rstrip('/')}/tasksets/{name}/import", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
            try:
                with urllib.request.urlopen(req, timeout=120) as r: out = json.load(r)
                print(f"imported {repo}/{tid} -> {out.get('taskset')} (check batch {out.get('check_batch')})"); n_ok += 1
            except urllib.error.HTTPError as e:
                print(f"failed {repo}/{tid}: {e.code} {e.read()[:200].decode(errors='replace')}"); n_bad += 1
    print(f"done: {n_ok} imported, {n_bad} skipped or failed"); return 0 if n_bad == 0 else 1


if __name__ == "__main__": sys.exit(main())
