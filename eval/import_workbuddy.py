#!/usr/bin/env python3
"""Import Tencent WorkBuddy-Bench Code tasks (the set Harness², arXiv 2610.14224, evolves on) into a taskset (CONTRACT §6).
usage: import_workbuddy.py <extracted wb-bench-code-v1.0 dir> <out taskset dir> [--name workbuddy-code] [--families script_verifier]

WorkBuddy-Bench v1.0 (Tencent, arXiv 2607.20911; license: Tencent, not for use within the EU). One task directory holds
instruction.md (an informal request, Chinese), task.toml, environment/{Dockerfile, workspace.tar.gz}, tests/{verifier.*, gold.patch}.
Three verifier families:
  script_verifier   (54) a standalone tests/verifier.py that imports the workspace and writes reward.json {overall: pass fraction}
  pytest_injected   (22) real open-source repos with pip-installed dependencies and injected pytest files  -> needs per-repo env layers; skipped here
  repo_understanding (4) a scorer over a written report                                                  -> skipped
This importer takes the families given (default script_verifier) whose Dockerfile installs nothing beyond the base image, so our
dev-tools env layer (python3, pytest, git) runs them. Per task:
  prompt.md   instruction.md + where the checkout is
  init/       workspace.tar.gz extracted (the agent's working tree)
  verify/     verifier.py + verify.sh: WORKSPACE=$1 python3 -I verifier.py -> reward.json.overall -> SCORE
  reference/  init with tests/gold.patch applied (import checks need a passing reference)
Groups: 60/20/20 by hash of the id; difficulty "easy" marks smoke tasks.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tarfile, tempfile
try: import tomllib
except ModuleNotFoundError: tomllib = None          # Python < 3.11 (the CI image): the importer only runs on a workstation; tests that need it skip

VERIFY_SH = r'''#!/usr/bin/env bash
# $1 = workspace. Runs the task's standalone verifier against it; last line is `SCORE x` (= reward.overall).
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; WS="$(cd "$1" && pwd)"
L="$(mktemp -d)"; mkdir -p "$L/bin"; ln -s "$(command -v python3)" "$L/bin/python"     # the verifiers call `python`
cd "$WS" && PATH="$L/bin:$PATH" WORKSPACE="$WS" LOG_DIR="$L" PYTHONDONTWRITEBYTECODE=1 python3 -I "$D/verifier.py" > "$L/out.txt" 2>&1
tail -20 "$L/out.txt"
S=$(python3 -I -c "import json,sys; print(round(float(json.load(open(sys.argv[1]))['overall']),4))" "$L/reward.json" 2>/dev/null || echo 0)
rm -rf "$L"; echo "SCORE $S"
'''


WORKSPACE_MAX = 40 << 20      # bytes; a sandbox gets the workspace through PUT /files, which cannot carry more than this reliably


def workspace_too_large(size, limit=WORKSPACE_MAX) -> bool:
    """Whether a workspace exceeds the file-channel size limit."""
    return size > limit


def extract_tar(path, dest):
    with tarfile.open(path) as tf:
        tf.extractall(dest, filter="data")


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("src"); ap.add_argument("out"); ap.add_argument("--name", default="workbuddy-code")
    ap.add_argument("--version", default="1"); ap.add_argument("--families", default="script_verifier"); ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    fams = set(a.families.split(",")); root = os.path.join(a.src, "tasks")
    os.makedirs(a.out, exist_ok=True); ids = []; skipped = {}
    for n in sorted(os.listdir(root))[: a.limit or None]:
        d = os.path.join(root, n)
        vt = tomllib.load(open(os.path.join(d, "tests", "verifier.toml"), "rb")); tt = tomllib.load(open(os.path.join(d, "task.toml"), "rb"))
        fam = vt.get("family"); dockerfile = open(os.path.join(d, "environment", "Dockerfile")).read()
        if fam not in fams: skipped[n] = f"family {fam}"; continue
        if re.search(r"pip install|npm |apt-get install(?!.*git curl bash ca-certificates xz-utils procps)", dockerfile.replace("\\\n", " ")) and "RUN python -m pip" in dockerfile:
            skipped[n] = "needs dependencies"; continue
        if not os.path.exists(os.path.join(d, "tests", "verifier.py")): skipped[n] = "no verifier.py"; continue
        tid = f"wb-{n}"; td = os.path.join(a.out, tid)
        shutil.rmtree(td, ignore_errors=True); os.makedirs(os.path.join(td, "verify")); os.makedirs(os.path.join(td, "reference"))
        extract_tar(os.path.join(d, "environment", "workspace.tar.gz"), os.path.join(td, "init"))
        shutil.rmtree(os.path.join(td, "init", ".git"), ignore_errors=True)
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(os.path.join(td, "init")) for f in fs)
        if workspace_too_large(size):                                                  # the CP→guest file channel truncates workspaces this large (history/evolve-2026-11-01: audio_scan, header_dimensions)
            skipped[n] = f"workspace {size >> 20} MiB over the {WORKSPACE_MAX >> 20} MiB file-channel limit"; shutil.rmtree(td); continue
        shutil.copy2(os.path.join(d, "tests", "verifier.py"), os.path.join(td, "verify", "verifier.py"))
        with open(os.path.join(td, "verify", "verify.sh"), "w") as f: f.write(VERIFY_SH)
        os.chmod(os.path.join(td, "verify", "verify.sh"), 0o755)
        # reference = init + gold.patch (git apply in a scratch repo so new files and renames work)
        with tempfile.TemporaryDirectory() as tmp:
            r = os.path.join(tmp, "r"); shutil.copytree(os.path.join(td, "init"), r)
            subprocess.run(["git", "-C", r, "init", "-q"], check=True)
            ok = subprocess.run(["git", "-C", r, "apply", "--whitespace=nowarn", os.path.join(d, "tests", "gold.patch")], capture_output=True, text=True)
            if ok.returncode != 0:
                ok = subprocess.run(["patch", "-p1", "-d", r, "-i", os.path.join(d, "tests", "gold.patch"), "-s"], capture_output=True, text=True)
            if ok.returncode != 0: skipped[n] = "gold patch does not apply: " + ok.stderr[-120:].strip(); shutil.rmtree(td); continue
            shutil.rmtree(os.path.join(r, ".git"))
            shutil.rmtree(os.path.join(td, "reference")); shutil.copytree(r, os.path.join(td, "reference"))
        m = tt.get("metadata", {})
        instr = open(os.path.join(d, "instruction.md")).read().strip()
        prompt = (f"{instr}\n\n---\n仓库的工作树就在当前目录(原任务里叫 `/workspace`,这里以当前目录为根)。直接修改这里的文件;"
                  f"验收是隐藏的功能测试,会导入或运行这个工作树里的代码,请保持已有的公开接口与文件结构。\n")
        open(os.path.join(td, "prompt.md"), "w").write(prompt)
        h = int(hashlib.sha256(tid.encode()).hexdigest(), 16) % 100
        group = "evolve" if h < 60 else ("validate" if h < 80 else "test")
        meta = {"id": tid, "domain": "coding", "group": group, "timeout_s": 900, "allowed_write": ["./"], "smoke": m.get("difficulty") == "easy",
                "source": {"benchmark": "workbuddy-bench-code-v1.0", "case": n, "difficulty": m.get("difficulty"), "category": m.get("category"),
                           "complexity": m.get("complexity"), "repo": m.get("repo"), "verification_mode": m.get("verification_mode"), "family": fam}}
        json.dump(meta, open(os.path.join(td, "meta.json"), "w"), indent=1, ensure_ascii=False)
        ids.append(tid)
    # smoke tasks: no "easy" task survives the family filter, so take the three smallest medium workspaces
    sizes = []
    for tid in ids:
        meta = json.load(open(os.path.join(a.out, tid, "meta.json")))
        if meta["source"].get("difficulty") == "medium":
            sizes.append((sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(os.path.join(a.out, tid, "init")) for f in fs), tid))
    for _, tid in sorted(sizes)[:3]:
        mp = os.path.join(a.out, tid, "meta.json"); meta = json.load(open(mp)); meta["smoke"] = True; json.dump(meta, open(mp, "w"), indent=1, ensure_ascii=False)
    json.dump({"name": a.name, "version": a.version, "source": "Tencent/workbuddy-bench wb-bench-code-v1.0 (Tencent license: not for use within the EU); script_verifier family without extra dependencies",
               "tasks": ids}, open(os.path.join(a.out, "taskset.json"), "w"), indent=1)
    print(f"{len(ids)} tasks -> {a.out}; skipped {len(skipped)}: " + ", ".join(sorted(set(skipped.values()))))
    for k, v in sorted(skipped.items()):
        if "patch" in v or "verifier" in v: print("  ", k, v)
    return 0


if __name__ == "__main__": sys.exit(main())
