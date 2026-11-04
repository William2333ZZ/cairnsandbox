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

import tarfile
script = os.path.join(WS, "bin", "rotate.sh")
case("script exists", os.path.exists(script))
tmp = tempfile.mkdtemp(); src = os.path.join(tmp, "src"); dest = os.path.join(tmp, "dest"); os.makedirs(src)
open(os.path.join(src, "a.txt"), "w").write("hello\n"); os.makedirs(os.path.join(src, "sub")); open(os.path.join(src, "sub", "b.txt"), "w").write("world\n")
def run(*args):
    return subprocess.run(["bash", script] + list(args), cwd=tmp, capture_output=True, text=True, timeout=60)
p = run(src, dest); case("argc -> 2 + stderr", p.returncode == 2 and p.stderr.strip() != "", f"rc={p.returncode}")
p = run(os.path.join(tmp, "nope"), dest, "3"); case("missing SRC -> 3", p.returncode == 3, f"rc={p.returncode}")
case("bad KEEP -> 4", all(run(src, dest, k).returncode == 4 for k in ("0", "-1", "abc", "")))
p = run(src, dest, "2")
first = os.path.join(dest, "backup-0001.tar.gz")
case("first archive created + printed", p.returncode == 0 and os.path.exists(first) and p.stdout.strip() == first, f"rc={p.returncode} out={p.stdout.strip()!r}")
ok = False
if os.path.exists(first):
    with tarfile.open(first) as tf:
        files = [m for m in tf.getmembers() if m.isfile() and not os.path.basename(m.name).startswith("._")]   # ignore macOS AppleDouble
        names = {os.path.normpath(m.name) for m in files}
        ok = names == {"a.txt", os.path.join("sub", "b.txt")} and tf.extractfile([m for m in files if m.name.endswith("a.txt")][0]).read() == b"hello\n"
case("archive contents", ok)
open(os.path.join(dest, "keep.me"), "w").write("x")
for _ in range(3): run(src, dest, "2")
left = sorted(f for f in os.listdir(dest))
case("rotation keeps newest 2 + other files", left == ["backup-0003.tar.gz", "backup-0004.tar.gz", "keep.me"], str(left))
finish()
