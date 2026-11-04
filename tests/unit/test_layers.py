"""Layering (docs/research/open-source-release-2026-11.md §3): the agent layer (aios/, intake/, eval/) reaches the sandbox cloud only through its HTTP API or the SDK,
and the sandbox layer (node/, guest/, sdk/) never imports the agent layer. The control plane may forward to the optional agent services over HTTP, nothing more."""
import os, re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
AGENT = ["aios", "intake", "eval"]
SANDBOX_PY = ["node", "sdk"]
NODE_MODULES = {os.path.splitext(f)[0] for f in os.listdir(os.path.join(ROOT, "node")) if f.endswith(".py")}
AGENT_MODULES = {os.path.splitext(f)[0] for d in AGENT for f in os.listdir(os.path.join(ROOT, d)) if f.endswith(".py")} | set(AGENT)
IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def py_files(dirs):
    for d in dirs:
        for base, ds, fs in os.walk(os.path.join(ROOT, d)):
            ds[:] = [x for x in ds if x not in ("node_modules", "tasksets", "__pycache__")]
            for f in fs:
                if f.endswith(".py"): yield os.path.join(base, f)


def test_agent_layer_does_not_import_node_internals():
    bad = []
    for p in py_files(AGENT):
        own = {os.path.splitext(f)[0] for f in os.listdir(os.path.dirname(p)) if f.endswith(".py")}
        for m in IMPORT.findall(open(p, encoding="utf-8").read()):
            if m in NODE_MODULES and m not in own: bad.append((os.path.relpath(p, ROOT), m))
        if re.search(r"""sys\.path[^\n]*(['"/]node['"/]|\.\./node|\.\./cp)""", open(p, encoding="utf-8").read()): bad.append((os.path.relpath(p, ROOT), "sys.path into node/cp"))
    assert not bad, bad


def test_sandbox_layer_does_not_import_agent_layer():
    bad = []
    for p in py_files(SANDBOX_PY):
        own = {os.path.splitext(f)[0] for f in os.listdir(os.path.dirname(p)) if f.endswith(".py")}
        for m in IMPORT.findall(open(p, encoding="utf-8").read()):
            if m in AGENT_MODULES and m not in own: bad.append((os.path.relpath(p, ROOT), m))
    assert not bad, bad
