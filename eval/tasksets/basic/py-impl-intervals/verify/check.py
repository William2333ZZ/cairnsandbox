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

def _raises(fn):
    try: fn(); return False
    except ValueError: return True
    except Exception: return False
ref = load(os.path.join(HERE, "ref_impl.py"), "intervals_ref")
cand = load(os.path.join(WS, "intervals.py"), "intervals_ws")
rng = random.Random(int.from_bytes(os.urandom(4), "big"))   # variant inputs on every verification
def rand_case():
    n = rng.randint(0, 8)
    ivs = []
    for _ in range(n):
        s = rng.randint(-5, 40); ivs.append([s, s + rng.randint(0, 12)])
    return ivs
def same(fn, args):
    try: return fn(cand)(*[json.loads(json.dumps(a)) for a in args]) == fn(ref)(*args)
    except Exception as e: print("  raised", type(e).__name__, e); return False
fixed = [[], [[1, 3], [3, 5]], [[5, 5], [1, 2]], [[0, 10], [2, 3]], [[1, 2], [4, 5], [7, 8]]]
cases = fixed + [rand_case() for _ in range(30)]
case("merge", all(same(lambda m: m.merge, (c,)) for c in cases))
case("merge does not mutate input", (lambda c: (cand.merge(c), c)[1] == [[4, 6], [1, 3]])([[4, 6], [1, 3]]))
case("merge rejects start>end", _raises(lambda: cand.merge([[3, 1]])))
case("total_length", all(same(lambda m: m.total_length, (c,)) for c in cases))
gcases = [(c, lo, lo + rng.randint(0, 50)) for c in cases for lo in [rng.randint(-10, 30)]]
case("gaps", all(same(lambda m: m.gaps, g) for g in gcases))
case("gaps rejects lo>hi", _raises(lambda: cand.gaps([], 5, 1)))
finish()
