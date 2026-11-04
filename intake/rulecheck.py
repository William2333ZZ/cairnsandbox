# Rules with teeth (docs/005 纠正, TRACE 2026): a remembered correction is advice -- the agent can read it and still break it (in the TRACE study,
# 57% of preference checks stayed violated with a memory layer alone). So a correction that can be stated as a check on the change is *compiled*:
# before a job may finish, the host runs the person's checks on its patch; a violation goes back to the agent as an input and the job does it
# again, up to twice. The checks are three kinds, deterministic, written by a model once and then fixed; none of them runs model-written code.
import re

KINDS = ("path_forbid", "new_file_regex", "added_forbid")


def compile_check(c):
    """A proposed check -> a validated {kind, pattern} or None. The pattern must compile, be short, and not match the empty string (a rule that fires on everything is not a rule)."""
    if not isinstance(c, dict) or c.get("kind") not in KINDS: return None
    pat = str(c.get("pattern") or "")
    if not pat or len(pat) > 200: return None
    if re.search(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*[+*{]", pat) or re.search(r"\\[1-9]", pat) or re.search(r"\(\?[<=!P]", pat): return None     # nested quantifiers, back-references and look-arounds are how a regex takes seconds on one line
    try: r = re.compile(pat)
    except re.error: return None
    if r.search(""): return None                                                       # matches nothing in particular, so it would fire on everything
    if c["kind"] != "new_file_regex" and all(r.search(s) for s in ("a/b.py", "x.md", "README", "src/x/y.txt", "print(1)", "docs/001.md")): return None   # a forbid that matches every sample forbids everything
    return {"kind": c["kind"], "pattern": pat}


def patch_facts(patch):
    """What a format-patch says: every path touched, the paths that are new, and the added lines with their path."""
    text = patch.decode("utf-8", "replace") if isinstance(patch, (bytes, bytearray)) else (patch or "")
    files, added_files, added = [], [], []
    cur, new_file = None, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', line); cur = m.group(2) if m else None; new_file = False
            for p in ((m.group(1), m.group(2)) if m else ()):                                  # both names: a rename out of a directory, a deletion and a mode change all name the old path
                if p not in files: files.append(p)
        elif line.startswith("--- /dev/null"): new_file = True
        elif line.startswith("+++ b/"):
            cur = line[6:].strip()
            if new_file and cur not in added_files: added_files.append(cur)
            if cur not in files: files.append(cur)
        elif line.startswith("+") and not line.startswith("+++") and cur: added.append((cur, line[1:1001]))      # a very long line (a minified file) is looked at in its first 1000 characters
    return {"files": files, "added_files": added_files, "added": added}


def _check_one(r, facts):
    c = r.get("chk") or {}
    try: pat = re.compile(c.get("pattern", ""))
    except re.error: return []
    if c.get("kind") == "path_forbid":
        hit = [p for p in facts["files"] if pat.search(p)]
        return [{"rule": r["text"], "why": f"改到了 {', '.join(hit[:3])}"}] if hit else []
    if c.get("kind") == "new_file_regex":
        bad = [p for p in facts["added_files"] if not pat.search(p)]
        return [{"rule": r["text"], "why": f"新文件 {', '.join(bad[:3])} 不合规矩"}] if bad else []
    if c.get("kind") == "added_forbid":
        hit = [p for p, line in facts["added"] if pat.search(line)]
        return [{"rule": r["text"], "why": f"新增的行里有不该有的东西({hit[0]})"}] if hit else []
    return []


def _in_child(conn, r, facts):
    try: conn.send(_check_one(r, facts))
    finally: conn.close()


def violations(rules, facts, timeout=2.0):
    """rules: [{id?, text, chk:{kind, pattern}}] -> [{rule, why, id?, slow?}] for the ones the change breaks. A model-written pattern can still be slow on some input no static check foresees, so each rule's check runs
    in its own child process with a time limit: one that does not finish is reported as `slow` (the caller switches its check off) and never freezes the service."""
    import multiprocessing as mp
    out = []
    for r in rules:
        if not r.get("chk"): continue
        ctx = mp.get_context("fork"); a, b = ctx.Pipe(duplex=False)
        p = ctx.Process(target=_in_child, args=(b, r, facts), daemon=True); p.start(); b.close()
        res = None
        if a.poll(timeout):
            try: res = a.recv()
            except EOFError: res = []
        p.join(0.1)
        if p.is_alive(): p.terminate(); p.join(1)
        a.close()
        if res is None: out.append({"rule": r["text"], "why": f"这条规则的检查在 {timeout:g} 秒内没跑完(写得太复杂),已停用它的检查", "id": r.get("id"), "slow": True})
        else: out += [{**v, "id": r.get("id")} for v in res]
    return out
