#!/usr/bin/env python3
"""Import Exercism exercises from Aider's polyglot-benchmark into a taskset (CONTRACT §6).
usage: import_polyglot.py <polyglot-benchmark checkout> <out taskset dir> [--lang python] [--subset small.json,medium.json] [--name polyglot-py]

The Polyglot benchmark is the public set DGM (arXiv 2505.22954) evolves on; here it is an external coordinate and a
small-scale validation set (docs/007-evaluation.md), not the optimisation target. One exercise -> one task:
  prompt.md    .docs/instructions*.md + which files to edit; tests are hidden
  init/        the stub solution file(s) and any other non-test, non-meta files
  verify/      the test file(s) + verify.sh running pytest; SCORE = fraction of tests passed (1.0 = all)
  reference/   .meta/example.* renamed onto the solution file(s)
Groups: a stable 60/20/20 split by hash of the id (evolve/validate/test); the DGM "small" subset marks smoke tasks.
Only Python is imported for now: the env layer has python3 + pytest; other languages need their toolchains in env.
"""
import argparse, hashlib, json, os, shutil, sys

VERIFY_SH = r'''#!/usr/bin/env bash
# $1 = workspace. Copies the hidden tests next to the solution and runs pytest; last line is `SCORE x`.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; WS="$(cd "$1" && pwd)"
T="$(mktemp -d)"; cp -R "$WS"/. "$T"/; find "$D" -maxdepth 1 -type f ! -name verify.sh -exec cp {} "$T"/ \;    # every hidden test file, helpers included
cd "$T"
OUT="$(python3 -I -m pytest -q -p no:cacheprovider 2>&1 | tail -40)"       # the verify sandbox already bounds wall time
echo "$OUT" | tail -15
P=$(echo "$OUT" | grep -Eo '[0-9]+ passed' | tail -1 | grep -Eo '[0-9]+' || echo 0)
F=$(echo "$OUT" | grep -Eo '[0-9]+ (failed|error)' | awk '{s+=$1} END{print s+0}')
N=$((P + F)); rm -rf "$T"
if [ "$N" -eq 0 ]; then echo "SCORE 0"; exit 0; fi
python3 -c "print('SCORE', round($P/$N, 4))"
'''


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("src"); ap.add_argument("out"); ap.add_argument("--lang", default="python")
    ap.add_argument("--subset", default=""); ap.add_argument("--name", default="polyglot-py"); ap.add_argument("--version", default="1")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    if a.lang != "python": print("only python is supported in this version", file=sys.stderr); return 2
    smoke = set()                                                   # only the first subset file (DGM "small") marks smoke tasks
    for f in [s for s in a.subset.split(",") if s][:1]:
        smoke |= {x.split("__", 1)[1] for x in json.load(open(f)) if x.startswith(a.lang + "__")}
    root = os.path.join(a.src, a.lang, "exercises", "practice")
    names = sorted(os.listdir(root))[: a.limit or None]
    os.makedirs(a.out, exist_ok=True); ids = []
    for n in names:
        d = os.path.join(root, n); cfg = json.load(open(os.path.join(d, ".meta", "config.json")))
        files = cfg.get("files", {}); sol, tests, examples = files.get("solution", []), files.get("test", []), files.get("example", [])
        if not sol or not tests or not examples: continue
        tid = f"py-{n}"; td = os.path.join(a.out, tid)
        shutil.rmtree(td, ignore_errors=True); os.makedirs(os.path.join(td, "init")); os.makedirs(os.path.join(td, "verify")); os.makedirs(os.path.join(td, "reference"))
        for f in os.listdir(d):
            if f in tests or f.startswith("."): continue
            p = os.path.join(d, f)
            (shutil.copytree if os.path.isdir(p) else shutil.copy2)(p, os.path.join(td, "init", f))
        for f in tests: shutil.copy2(os.path.join(d, f), os.path.join(td, "verify", f))
        for s, e in zip(sol, examples): shutil.copy2(os.path.join(d, e), os.path.join(td, "reference", s))
        with open(os.path.join(td, "verify", "verify.sh"), "w") as f: f.write(VERIFY_SH)
        os.chmod(os.path.join(td, "verify", "verify.sh"), 0o755)
        docs = ""
        for df in ("introduction.md", "instructions.md", "instructions.append.md"):
            p = os.path.join(d, ".docs", df)
            if os.path.exists(p): docs += open(p).read().strip() + "\n\n"
        prompt = (f"# {n}\n\n{docs}## Task\n\nImplement the solution in `{'`, `'.join(sol)}` (the stub is already there). "
                  f"Hidden unit tests exercise the public functions and classes named in the stub; keep their names and signatures. "
                  f"You may add your own tests, but do not rely on any test file being present. Only edit `{'`, `'.join(sol)}`.\n")
        open(os.path.join(td, "prompt.md"), "w").write(prompt)
        h = int(hashlib.sha256(tid.encode()).hexdigest(), 16) % 100
        group = "evolve" if h < 60 else ("validate" if h < 80 else "test")
        meta = {"id": tid, "domain": "coding", "group": group, "timeout_s": 300, "allowed_write": sol, "smoke": n in smoke,
                "source": {"benchmark": "polyglot-benchmark", "language": a.lang, "exercise": n, "blurb": cfg.get("blurb", "")}}
        json.dump(meta, open(os.path.join(td, "meta.json"), "w"), indent=1)
        ids.append(tid)
    json.dump({"name": a.name, "version": a.version, "source": "Aider-AI/polyglot-benchmark (Exercism); DGM subsets mark smoke", "tasks": ids}, open(os.path.join(a.out, "taskset.json"), "w"), indent=1)
    print(f"{len(ids)} tasks -> {a.out}; smoke={sum(1 for t in ids if json.load(open(os.path.join(a.out, t, 'meta.json')))['smoke'])}")
    return 0


if __name__ == "__main__": sys.exit(main())
