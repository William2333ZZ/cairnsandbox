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

m = load(os.path.join(WS, "slugify.py"), "slugify_ws")
f = m.slugify
cases = [("Hello, World!", "hello-world"), ("  --Leading and trailing--  ", "leading-and-trailing"),
         ("Crème brûlée à la mode", "creme-brulee-a-la-mode"), ("multiple   spaces___and___underscores", "multiple-spaces-and-underscores"),
         ("", ""), ("!!!", ""), ("UPPER lower MiXeD 123", "upper-lower-mixed-123"), ("snake_case_name.ext", "snake-case-name-ext")]
for inp, exp in cases:
    try: got = f(inp)
    except Exception as e: got = f"<raised {type(e).__name__}>"
    case(f"slugify({inp!r})", got == exp, f"got {got!r} want {exp!r}")
case("max_len cut strips dash", f("abc def ghi", max_len=4) == "abc", repr(f("abc def ghi", max_len=4)))
case("max_len exact", len(f("a" * 80, max_len=10)) == 10)
finish()
