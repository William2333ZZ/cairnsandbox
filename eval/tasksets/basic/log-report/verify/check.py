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

exp = open(os.path.join(HERE, "expected_report.txt")).read().splitlines()
try: got = open(os.path.join(WS, "report.txt")).read().splitlines()
except OSError: got = []
got = [g.rstrip() for g in got if g.strip()]
for i, e in enumerate(exp):
    case(e.split()[0], i < len(got) and got[i] == e, f"got {got[i] if i < len(got) else None!r}")
finish()
