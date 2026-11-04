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
try: got = json.load(open(os.path.join(WS, "summary.json")))
except Exception as e: got = {}; print("cannot read summary.json:", e)
if not isinstance(got, dict): got = {}
for c, v in exp.items():
    g = got.get(c) or {}
    case(c, g.get("orders") == v["orders"] and isinstance(g.get("total"), (int, float)) and abs(g.get("total", -1) - v["total"]) < 0.011, str(g))
case("no extra customers", set(got) <= set(exp), str(sorted(set(got) - set(exp))))
finish()
