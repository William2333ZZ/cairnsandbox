import hashlib, importlib.util, json, os, random, re, shutil, subprocess, sys, tempfile
WS = os.path.abspath(sys.argv[1]); HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = []
def case(name, ok, note=""):
    RESULTS.append(bool(ok)); print(f"[{'ok' if ok else 'FAIL'}] {name} {note}"[:300])
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m); return m
def finish():
    print(f"SCORE {sum(RESULTS) / len(RESULTS) if RESULTS else 0:.4f}")

import configparser
conf_txt = open(os.path.join(WS, "app.conf")).read() if os.path.exists(os.path.join(WS, "app.conf")) else ""
cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
try: cp.read_string(conf_txt)
except Exception as e: print("conf parse error", e)
case("server.port=8080", cp.get("server", "port", fallback="") == "8080")
case("use_ssl2 removed", "use_ssl2" not in conf_txt and cp.get("server", "workers", fallback="") == "4" and cp.get("server", "host", fallback="") == "0.0.0.0")
case("[logging] appended", cp.get("logging", "level", fallback="") == "info" and cp.get("logging", "file", fallback="") == "/var/log/app.log"
     and list(cp.sections()) == ["server", "database", "logging"] and cp.get("database", "pool", fallback="") == "5")
orig = open(os.path.join(HERE, "app.conf.orig")).read()
comments = lambda t: [l for l in t.splitlines() if l.startswith("#")]
case("comments preserved", comments(conf_txt) == comments(orig))
readme = open(os.path.join(WS, "README.md")).read() if os.path.exists(os.path.join(WS, "README.md")) else ""
exp = open(os.path.join(HERE, "README.md.orig")).read().replace("## Setup", "## Installation").replace("localhost:3000", "localhost:8080")
case("README heading", "## Installation" in readme and "## Setup" not in readme)
case("README exact", readme.strip() == exp.strip())
finish()
