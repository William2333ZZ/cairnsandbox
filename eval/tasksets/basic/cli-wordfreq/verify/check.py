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

import collections
script = os.path.join(WS, "wordfreq.py")
case("script exists", os.path.exists(script))
tmp = tempfile.mkdtemp()
def expected(text, n, L):
    c = collections.Counter(w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= L)
    return "\n".join(f"{w} {k}" for w, k in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:n])
def run(args):
    return subprocess.run([sys.executable, "-I", script] + args, cwd=tmp, capture_output=True, text=True, timeout=30)
texts = {"a.txt": "The quick brown fox jumps over the lazy dog. The dog sleeps; the fox doesn't.\n",
         "b.txt": "Alpha beta GAMMA, alpha-beta gamma! 42 alpha's bb aa aa cc\n" * 3,
         "c.txt": "", "d.txt": "it's it's its ITS it s, tie tie tie\nzoo apple apple zoo\n"}
for name, t in texts.items(): open(os.path.join(tmp, name), "w").write(t)
for name, args, n, L in [("a.txt", ["-n", "3"], 3, 1), ("b.txt", [], 10, 1), ("b.txt", ["--min-len", "3", "-n", "2"], 2, 3),
                         ("c.txt", [], 10, 1), ("d.txt", ["-n", "4", "--min-len", "2"], 4, 2)]:
    p = run(args + [os.path.join(tmp, name)])
    case(f"{name} {args}", p.returncode == 0 and p.stdout.strip() == expected(texts[name], n, L), repr(p.stdout[:120]))
p = run([os.path.join(tmp, "missing.txt")])
case("missing file -> exit 1, stderr, empty stdout", p.returncode == 1 and p.stderr.strip() and not p.stdout.strip())
finish()
