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

exp = json.load(open(os.path.join(HERE, "expected.json")))
got = {}
for root, _, files in os.walk(os.path.join(WS, "sorted")):
    for f in files:
        p = os.path.join(root, f); got[os.path.relpath(p, WS)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
correct = sum(1 for k, h in exp.items() if got.get(k) == h)
extra = [k for k in got if k not in exp]
for k, h in exp.items(): case(k, got.get(k) == h)
print("extra files:", extra)
score = max(0.0, (correct - len(extra)) / len(exp))
print(f"SCORE {score:.4f}")
