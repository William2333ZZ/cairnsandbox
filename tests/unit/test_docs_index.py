"""docs/011-architecture.md is the code index: every module and test file must appear in it, and the numbered
docs must only link backwards (a doc may cite earlier docs, never later ones)."""
import os, re, glob
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS = os.path.join(ROOT, "docs")
pytestmark = pytest.mark.skipif(not os.path.isdir(DOCS), reason="docs/ not present")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _sources():
    pats = ["node/*.py", "node/*.sh", "node/guest/*", "eval/*.py", "intake/*.py", "aios/*.py", "guest/sbx-runtime/src/*.rs",
            "web/*.js", "web/*.html", "web/*.py", "tests/unit/*.py", "run-node.sh", "recipes/*/", "cp/*.go", "cp/internal/*/*.go", "cp/Dockerfile", "sdk/python/*/*.py", "sdk/python/examples/*.py", "deploy/*", "ops/*.py"]
    out = []
    for pat in pats:
        for p in glob.glob(os.path.join(ROOT, pat)):
            rel = os.path.relpath(p, ROOT).rstrip("/")
            if "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            out.append(rel)
    return sorted(set(out))


def test_every_module_is_indexed():
    idx = _read(os.path.join(DOCS, "011-architecture.md"))
    missing = []
    for rel in _sources():
        base = os.path.basename(rel)
        if rel in idx or base in idx or (rel + "/") in idx:
            continue
        missing.append(rel)
    assert not missing, "not listed in docs/011-architecture.md: %s" % missing


def test_numbered_docs_only_link_backwards():
    bad = []
    for p in sorted(glob.glob(os.path.join(DOCS, "0*.md"))):
        n = int(os.path.basename(p)[:3])
        for m in re.findall(r"\((0\d\d)-[a-z-]+\.md", _read(p)):
            if int(m) > n:
                bad.append((os.path.basename(p), m))
    assert not bad, "forward references: %s" % bad


def test_no_broken_relative_links():
    bad = []
    for p in glob.glob(os.path.join(DOCS, "**", "*.md"), recursive=True):
        if os.sep + "archive" + os.sep in p:
            continue
        for m in re.finditer(r"\]\(([^)#\s]+)(#[^)]*)?\)", _read(p)):
            target = m.group(1)
            if target.startswith("http"):
                continue
            q = os.path.normpath(os.path.join(os.path.dirname(p), target))
            if not os.path.exists(q):
                bad.append((os.path.relpath(p, ROOT), target))
    assert not bad, "broken links: %s" % bad
