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

import csv
script = os.path.join(WS, "csv2json.py")
case("script exists", os.path.exists(script))
rng = random.Random(int.from_bytes(os.urandom(4), "big"))   # variant inputs on every verification
ref = load(os.path.join(HERE, "ref_impl.py"), "csv2json_ref")
tmp = tempfile.mkdtemp()
def gen(n):
    cols = ["id", " name", "qty ", "price", "flag", "note"]
    rows = []
    ids = rng.sample(range(1, 500), n)
    for i in ids:
        rows.append([str(i), rng.choice(["Ann", "Smith, Jane", " padded ", "", "O\'Neil", "multi\nline"]),
                     rng.choice(["3", "-7", "+2", "", "0012", "x1"]), rng.choice(["1.5", "1e3", "-.5", "", "3.", "abc"]),
                     rng.choice(["true", "FALSE", "True", "", "yes"]), rng.choice(["", "hi there", "\"quoted\"", "tab\tin"])])
    path = os.path.join(tmp, f"in{n}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(rows)
    return path
for n in (1, 5, 12, 25):
    src = gen(n); exp_out = os.path.join(tmp, f"exp{n}.json"); got_out = os.path.join(tmp, f"got{n}.json")
    ref.main(src, exp_out)
    p = subprocess.run([sys.executable, "-I", script, src, got_out], cwd=tmp, capture_output=True, text=True, timeout=60)
    try: got = json.load(open(got_out))
    except Exception as e: got = f"<{type(e).__name__}>"
    case(f"random csv n={n}", p.returncode == 0 and got == json.load(open(exp_out)), p.stderr[-200:])
finish()
