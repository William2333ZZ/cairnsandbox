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

tmp = tempfile.mkdtemp(); shutil.copytree(WS, tmp, dirs_exist_ok=True)
mk = subprocess.run(["make", "-s"], cwd=tmp, capture_output=True, text=True)
case("make builds", mk.returncode == 0 and os.path.exists(os.path.join(tmp, "stats")), mk.stderr[-300:])
if mk.returncode != 0: RESULTS.extend([False] * 4); finish(); sys.exit(0)
def run(inp):
    p = subprocess.run([os.path.join(tmp, "stats")], input=inp, capture_output=True, text=True, timeout=10)
    return p.stdout.strip()
for inp, exp in [("1 2 3 4", "count=4 sum=10 max=4"), ("-5 -2 -9", "count=3 sum=-16 max=-2"), ("", "count=0 sum=0 max=0"),
                 ("7", "count=1 sum=7 max=7")]:
    case(f"stats({inp!r})", run(inp) == exp, run(inp))
cl = subprocess.run(["make", "-s", "clean"], cwd=tmp, capture_output=True, text=True)
case("make clean", cl.returncode == 0 and not os.path.exists(os.path.join(tmp, "stats")))
finish()
