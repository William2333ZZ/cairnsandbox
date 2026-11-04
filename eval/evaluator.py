#!/usr/bin/env python3
"""fc-eval: the evaluator. Library + HTTP service implementing CONTRACT.md §3.

Pieces (all in this file, stdlib only):
  tasksets   load eval/tasksets/<name>/ (CONTRACT §6), tar helpers, import checks (§6.7 of the survey)
  backends   CPBackend (control-plane API, CONTRACT §1) and LocalBackend (subprocess, for tests / --local)
  Runner     one run = agent sandbox -> workspace tar -> fresh verify sandbox -> SCORE (survey §6.2)
  cheat      signals extracted from traj + workspace diff (survey §6.6); recorded, never change scores
  stats      avg@k, pass@1, pass^k, split-half noise band delta, paired bootstrap CI, McNemar (survey §6.3/6.4)
  Store      sqlite: versions, batches, runs (json columns)
  Evaluator  batch executor (thread pool, MAX_PARALLEL) + cascade (smoke -> full) for kind "compare"
  HTTP       ThreadingHTTPServer exposing §3 routes; CLI: serve | check | run | stats
"""
import argparse, hashlib, hmac, io, json, math, os, random, re, shutil, sqlite3, statistics, subprocess, sys
import tarfile, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKS_DIR = os.path.join(os.environ.get("EVAL_STATE", os.path.join(HERE, "state")), "checks")
DEFAULT_AGENT_CMD = "/aios/bin/aios run --task-file {prompt} --config /aios/harness/aios/config.json --out {out} --cwd {cwd}"   # agent-harness layer
VERIFY_TIMEOUT_S = 300
IGNORED_WRITE_RE = re.compile(r"(^|/)(__pycache__/|\.pytest_cache/|\.cache/)|\.pyc$")
INFRA_RETRIES = 3
PASS_EPS = 1e-9          # pass := score >= 1 - PASS_EPS (full credit)
MAX_WS_BYTES = 5 << 20   # cheat signal threshold on workspace size


def cfg_from_env():
    return {
        "port": int(os.environ.get("EVAL_PORT", "8600")),
        "state": os.environ.get("EVAL_STATE", os.path.join(HERE, "state")),
        "cp_url": os.environ.get("CP_URL", "http://127.0.0.1:18700").rstrip("/"),
        "cp_token": os.environ.get("CP_TOKEN") or (open(os.environ["CP_TOKEN_FILE"]).read().strip() if os.environ.get("CP_TOKEN_FILE") and os.path.exists(os.environ["CP_TOKEN_FILE"]) else ""),   # a compose / k8s secret file works too
        "model_proxy": os.environ.get("MODEL_PROXY_URL", "").rstrip("/"),
        "model_name": os.environ.get("CAIRN_MODEL_NAME", "deepseek-flash"),
        "budget_usd": float(os.environ.get("EVAL_BUDGET_USD", "1.0")),
        "tasksets": os.environ.get("TASKSETS_DIR", os.path.join(HERE, "tasksets")),
        "template": os.environ.get("EVAL_TEMPLATE", "ubuntu-24.04"),
        "layers": [l for l in os.environ.get("EVAL_LAYERS", "").split(",") if l],   # default layers (e.g. the dev layer) when a version has none
        "landlock": os.environ.get("EVAL_LANDLOCK", "1") == "1",                     # confine the agent step (agent-runtime Landlock)
        "max_parallel": max(1, min(4, int(os.environ.get("MAX_PARALLEL", "2")))),  # shared dev box: cap at 4
    }


def now(): return time.time()
def new_id(prefix): return f"{prefix}-{uuid.uuid4().hex[:10]}"
def is_pass(score): return score is not None and score >= 1.0 - PASS_EPS


# ----------------------------------------------------------------------------------------------- tasksets

def tar_dir(path, exclude=()):
    """Tar a directory's contents (relative paths, deterministic order) into bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in exclude)
            rel = os.path.relpath(root, path)
            if rel != "." and os.path.basename(root) not in exclude:
                tf.add(root, arcname=rel, recursive=False)
            for f in sorted(files):
                if f in exclude: continue
                tf.add(os.path.join(root, f), arcname=os.path.normpath(os.path.join(rel, f)), recursive=False)
    return buf.getvalue()


def tar_listing(data):
    """{relative path: sha256 of content (None for dirs/links)} for a tar in bytes."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf:
            name = os.path.normpath(m.name)
            if name in (".", "") or os.path.basename(name).startswith("._"): continue   # skip macOS AppleDouble entries
            if m.isfile():
                out[name] = hashlib.sha256(tf.extractfile(m).read()).hexdigest()
            else:
                out[name] = None
    return out


def rules_prompt(rules):
    """The same words the harness shows in a real job (aios/run.py rules_text; tests/unit pin the two together): a dependent task
    is passed with the rules the user taught, which is what the agent's memory would hand it."""
    if not rules: return ""
    return "\n\nThe user has corrected you before. Follow these unless this request says otherwise:" + "".join(f"\n- {r}" for r in rules)


ASK_RULE = "If you need a human decision, finish with a submission whose first line starts with QUESTION: and the question; ask at most once per ticket."   # the words of aios/run.py ticket_text
ASK_RULE_V2 = "If the request leaves out a decision that changes what you would build (a number, a name, a choice between two behaviours) and the repository does not settle it, ask before you build: finish with a submission whose first line starts with QUESTION: and the question. The person is there to answer: a question costs them one sentence, a wrong guess costs them a review round. Do not ask about anything you can find by reading. Ask at most once per ticket."   # the words of aios/run.py ASK_RULE_V2
ASK_RULES = {"v1": ASK_RULE, "v2": ASK_RULE_V2}
ASK_HOW = "To ask, your whole response is one command like this (its first output line must be the marker, the rest is the message): cat <<'EOF'\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nQUESTION: <your question>\nEOF"
MAX_ASKS = 2                                  # answered questions per run of an obstacle task; a third is left unanswered
MATCH_DICE = 0.2                              # token overlap above which a question counts as asking about a blocker
_STOP = set("the and for with that this you are can will would should could which what when where how why not but any all has have had was were been being get got let its our out per via too yet one two use used using need needs want wants new add want please also then than into onto from your their there here just only does did done may might shall".split())


def _tokens(s):
    """Words of a question (latin words and file names, minus filler) plus bigrams of its CJK characters: what two phrasings of one question share."""
    s = (s or "").lower()
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", s))
    return {w for w in re.findall(r"[a-z0-9_.\-]{3,}", s) if w not in _STOP} | {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def dice(a, b):
    A, B = _tokens(a), _tokens(b)
    return 2 * len(A & B) / (len(A) + len(B)) if A and B else 0.0


def question_of(traj):
    """The question an agent asked instead of finishing (docs/006: a submission whose first line starts with QUESTION:), or None."""
    sub = (((traj or {}).get("info") or {}).get("submission") or "").strip()
    return sub.split(":", 1)[1].strip()[:600] if sub.upper().startswith("QUESTION:") and ":" in sub else None


def match_blocker(q, blockers, used):
    """Index of the unused blocker this question is about (best overlap above MATCH_DICE), or None."""
    best, bi = 0.0, None
    for i, b in enumerate(blockers):
        if i in used: continue
        d = dice(q, b.get("question", ""))
        if d > best: best, bi = d, i
    return bi if best >= MATCH_DICE else None


def ask_metrics(asks, blockers):
    """Ask-F1 (HiL-Bench, 2026): precision = questions that were about a real blocker / questions asked; recall = blockers asked about / blockers.
    Asking nothing when something was missing is recall 0 and F1 0; asking about nothing real is precision 0."""
    n, B = len(asks), len(blockers)
    hit = len({a["blocker"] for a in asks if a.get("blocker") is not None})
    p = (sum(1 for a in asks if a.get("blocker") is not None) / n) if n else None
    r = (hit / B) if B else None
    if p is None or r is None: f1 = 0.0 if (not n and B) else None
    else: f1 = 2 * p * r / (p + r) if p + r > 0 else 0.0
    return {"asked": n, "matched": sum(1 for a in asks if a.get("blocker") is not None), "blockers": B, "hit": hit, "precision": p, "recall": r, "f1": f1}


SUBTASK_RE = re.compile(r"^SUBTASK\s+(\S+)\s+([0-9.]+)\s*$")


def parse_subtasks(stdout):
    """`SUBTASK <name> <0..1>` lines of a verify script: the parts of a long task, so a run's partial credit says which parts passed."""
    out = {}
    for line in (stdout or "").splitlines():
        m = SUBTASK_RE.match(line.strip())
        if m: out[m.group(1)] = max(0.0, min(1.0, float(m.group(2))))
    return out


SCORING_VERSION = "2"      # 1: raw per-file mean, cost at the cache-miss price. 2: lift over the no-op (D23), cost at the provider's cache-aware price (D22). A batch records it; batches of different versions are not compared.


def taskset_digest(base, ids):
    """The version of a task set: its tasks AND what they say. A task re-imported under the same id with a different prompt, hidden test or kind is a different measurement; if the version only
    followed the ids, a batch run before the change would be compared with one run after it as if nothing had moved."""
    h = hashlib.sha256()
    for tid in ids:
        h.update(tid.encode())
        td = os.path.join(base, tid)
        for rel in ("prompt.md", "meta.json"):
            try: h.update(open(os.path.join(td, rel), "rb").read())
            except OSError: pass
        for root, _, names in sorted(os.walk(os.path.join(td, "init"))):                       # the workspace: names and sizes are enough to notice a different one
            for n in sorted(names):
                try: h.update(os.path.join(root, n).encode()); h.update(str(os.path.getsize(os.path.join(root, n))).encode())
                except OSError: pass
        for root, _, names in sorted(os.walk(os.path.join(td, "verify"))):
            for n in sorted(names):
                try: h.update(n.encode()); h.update(open(os.path.join(root, n), "rb").read())
                except OSError: pass
    return h.hexdigest()[:8]


def lift_score(raw, now, noop):
    """Credit for what the change did, not for what was already true. A mined task is scored per test file (SUBTASK lines), and most of the files pass before anyone touches the
    tree: on seven of the user's own tasks the agent's score equalled the do-nothing score exactly (0.73 to 0.93), so the mean 0.651 measured the task set, not the agent.
    Files that fail before the change are the targets; each is worth the share of its remaining distance the run covered. A file that passed before and fails now takes one target's
    worth away. `raw` is returned when there is nothing to adjust (no per-file scores, or every file already passes)."""
    if not now or not noop or any(f not in now for f in noop): return raw                  # no per-file scores, or a no-op record of a different set of files (a task rewritten since it was checked): leave the raw score alone
    targets = [f for f, v in noop.items() if v < 1.0 - PASS_EPS]
    if not targets: return raw
    gain = sum(max(-1.0, min(1.0, (now.get(f, 0.0) - noop[f]) / (1.0 - noop[f]))) for f in targets)
    loss = sum(1.0 - now.get(f, 0.0) for f, v in noop.items() if v >= 1.0 - PASS_EPS and now.get(f, 0.0) < 1.0)
    return round(max(0.0, min(1.0, (gain - loss) / len(targets))), 4)


class Task:
    def __init__(self, tdir):
        self.dir = tdir
        self.meta = json.load(open(os.path.join(tdir, "meta.json")))
        self.id = self.meta.get("id") or os.path.basename(tdir)
        self.prompt = open(os.path.join(tdir, "prompt.md")).read()
        self.init_dir = os.path.join(tdir, "init")
        self.verify_dir = os.path.join(tdir, "verify")
        self.reference_dir = os.path.join(tdir, "reference")
        self.timeout_s = int(self.meta.get("timeout_s", 300))
        self.refresh_mined()
        self.kind = self.meta.get("kind") or "plain"                                   # plain | dependent | obstacle | long (docs/007 任务的种类)
        self.rules = [str(r) for r in (self.meta.get("rules") or [])]
        self.blockers = [b for b in (self.meta.get("blockers") or []) if b.get("question") and b.get("answer")]     # what a person would have to say; the prompt does not

    TRUSTED_CONFIG = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")

    def refresh_mined(self):
        """A mined task is graded by the current grading script, with the configuration files of the workspace as it was given (not the agent's): older tasks get both on load."""
        if not os.path.exists(os.path.join(self.verify_dir, "tests.txt")): return
        try:
            vs = os.path.join(self.verify_dir, "verify.sh")
            if not os.path.exists(vs) or open(vs).read() != MINED_VERIFY_SH:                       # only when it differs: a run may be reading it
                with open(vs, "w") as f: f.write(MINED_VERIFY_SH)
                os.chmod(vs, 0o755)
            trusted = os.path.join(self.verify_dir, "_trusted")
            if os.path.isdir(self.init_dir) and not os.path.isdir(trusted):
                for root, _, names in os.walk(self.init_dir):
                    if "node_modules" in root.split(os.sep): continue
                    for n in names:
                        if n in self.TRUSTED_CONFIG:
                            rel = os.path.relpath(os.path.join(root, n), self.init_dir); dest = os.path.join(trusted, rel); os.makedirs(os.path.dirname(dest), exist_ok=True); shutil.copy2(os.path.join(root, n), dest)
                os.makedirs(trusted, exist_ok=True)
        except OSError: pass                                                                      # a read-only task directory keeps what it has

    @property
    def checks_path(self):      # results live in the evaluator's state dir (the taskset mount may be read-only)
        return os.path.join(CHECKS_DIR, os.path.basename(os.path.dirname(self.dir)), self.id + ".json")

    @property
    def checks(self):
        p = self.checks_path
        return json.load(open(p)) if os.path.exists(p) else None

    @property
    def status(self):
        c = self.checks
        if c is None: return "unchecked"
        return "ok" if all(c.get(k) for k in ("reference_pass", "noop_fail", "repeat_consistent", "within_time")) else "invalid"

    def info(self):
        return {"id": self.id, "domain": self.meta.get("domain"), "group": self.meta.get("group"), "kind": self.kind, "n_rules": len(self.rules), "n_blockers": len(self.blockers),
                "timeout_s": self.timeout_s, "smoke": bool(self.meta.get("smoke")), "status": self.status,
                "checks": self.checks}

    def hidden_names(self):
        """Basenames of hidden test files under verify/ (excluding verify.sh)."""
        names = set()
        for root, _, files in os.walk(self.verify_dir):
            names.update(f for f in files if f != "verify.sh")
        return names


MINED_VERIFY_SH = r'''#!/usr/bin/env bash
# A task mined from the user's own work (docs/007 任务从哪来): the original change's test files are the hidden tests. $1 = workspace.
# They are copied over the agent's tree at their original paths and run with pytest; one `SUBTASK <test file> <share passed>` line per test file
# (the parts of a long task), then the last line `SCORE x` = tests passed / tests run (partial credit).
# The agent's tree does not get to configure or impersonate the run that grades it: its conftest / pytest configuration / anything named like pytest is removed and the
# task's own copies (from the workspace as it was given) are put back; results come from a junit file written outside the tree, not from text the tests could print.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; WS="$(cd "$1" && pwd)"
T="$(cd "$(mktemp -d)" && pwd -P)"; O="$(cd "$(mktemp -d)" && pwd -P)"                      # the real path: pytest names tests relative to it
cp -R "$WS"/. "$T"/
find "$T" \( -name conftest.py -o -name pytest.ini -o -name tox.ini -o -name setup.cfg -o -name pyproject.toml -o -name sitecustomize.py -o -name usercustomize.py \) -not -path "*/node_modules/*" -delete
rm -rf "$T"/pytest "$T"/_pytest "$T"/pytest.py "$T"/py "$T"/pluggy "$T"/iniconfig "$T"/packaging "$T"/.pytest_cache
[ -d "$D/_trusted" ] && cp -R "$D/_trusted"/. "$T"/
(cd "$D" && find . -type f ! -name verify.sh ! -name tests.txt -not -path "./_trusted/*" -print0 | while IFS= read -r -d '' f; do mkdir -p "$T/$(dirname "$f")"; cp "$f" "$T/$f"; done)
TESTS=(); while IFS= read -r line; do [ -n "$line" ] && TESTS+=("$line"); done < "$D/tests.txt"                        # (not mapfile: older bash has none)
if [ "${#TESTS[@]}" -eq 1 ] && [[ "${TESTS[0]}" == *" "* ]] && [ ! -e "$T/${TESTS[0]}" ]; then read -r -a TESTS <<< "${TESTS[0]}"; fi      # the older one-line format
cd "$T"
PYTHONPATH="$T" python3 -m pytest -q -p no:cacheprovider --rootdir "$T" -o junit_family=xunit2 --junitxml="$O/j.xml" "${TESTS[@]}" > "$O/out.txt" 2>&1   # tests import the repository's packages from its root
tail -15 "$O/out.txt"
cd /; python3 -I - "$O/j.xml" "${TESTS[@]}" <<'PY'
import collections, sys
import xml.etree.ElementTree as ET
files = sys.argv[2:]; per = collections.OrderedDict((f, [0, 0]) for f in files)
mods = {f: f[:-3].replace("/", ".") for f in files}
try: root = ET.parse(sys.argv[1]).getroot()
except Exception: root = None
if root is not None:
    for tc in root.iter("testcase"):
        mod = tc.get("classname") or tc.get("name") or ""
        f = max((f for f in files if mod == mods[f] or mod.startswith(mods[f] + ".")), key=lambda f: len(mods[f]), default=None)
        if f is None: continue
        per[f][1] += 1; per[f][0] += not any(c.tag in ("failure", "error", "skipped") for c in tc)       # a skipped test is not a passed one: it stays in the denominator
for f, v in per.items():
    if v[1] == 0: v[1] = 1                                                                                # a file that produced no result at all is one failure, not nothing
P, N = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
for f, (a, b) in per.items(): print("SUBTASK", f, round(a / b, 4))
print("SCORE", round(P / N, 4) if N else 0)
PY
rm -rf "$T" "$O"
'''


def taskset_dirs(cfg):
    """Built-in task sets ship read-only with the code; mined ones live in the state directory (docs/007 任务从哪来)."""
    return [cfg["tasksets"], os.path.join(cfg["state"], "tasksets")]


def load_taskset(tasksets_dir, name):
    dirs = tasksets_dir if isinstance(tasksets_dir, list) else [tasksets_dir]
    d = next((os.path.join(x, name) for x in dirs if os.path.exists(os.path.join(x, name, "taskset.json"))), os.path.join(dirs[0], name))
    ts = json.load(open(os.path.join(d, "taskset.json")))
    tasks = [Task(os.path.join(d, tid)) for tid in ts["tasks"]]
    return {"name": ts.get("name", name), "version": ts.get("version", "0"), "dir": d, "tasks": tasks}


def list_tasksets(tasksets_dir):
    out = []; seen = set()
    for base in (tasksets_dir if isinstance(tasksets_dir, list) else [tasksets_dir]):
      if not os.path.isdir(base): continue
      for name in sorted(os.listdir(base)):
        if name in seen or not os.path.exists(os.path.join(base, name, "taskset.json")): continue
        seen.add(name); ts = load_taskset(base, name)
        groups = {}
        for t in ts["tasks"]: groups[t.meta.get("group", "?")] = groups.get(t.meta.get("group", "?"), 0) + 1
        out.append({"name": ts["name"], "version": ts["version"], "n_tasks": len(ts["tasks"]), "groups": groups,
                    "checked": all(t.checks is not None for t in ts["tasks"])})
    return out


# ----------------------------------------------------------------------------------------------- backends

class LostRun(str):
    """The error text of a run that never finished, with the usage its failed attempts had already cost."""
    carried = {}


class InfraError(Exception):
    """Infrastructure failure: sandbox creation/transport/CP errors. Retried, never counted as a task failure."""


class CPBackend:
    """Sandbox operations over the control-plane API (CONTRACT §1). Paths are guest-absolute."""
    users = True   # guest has root + agent users

    def __init__(self, url, token=""):
        self.url, self.token = url.rstrip("/"), token

    def path(self, sid, p): return p

    def _req(self, method, path, body=None, raw=False, timeout=600):
        data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/octet-stream" if isinstance(body, (bytes, bytearray)) else "application/json")
        if self.token: req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return payload if raw else (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            raise InfraError(f"CP {method} {path} -> {e.code}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise InfraError(f"CP {method} {path} transport error: {e}") from None

    def create(self, spec):
        info = self._req("POST", "/sandboxes", spec)
        if not info.get("id"): raise InfraError(f"create returned no id: {info}")
        self.last_node = info.get("node")
        return info["id"]

    def put(self, sid, path, data):
        self._req("PUT", f"/sandboxes/{sid}/files?path={urllib.parse.quote(path)}", data)

    def get(self, sid, path):
        return self._req("GET", f"/sandboxes/{sid}/files?path={urllib.parse.quote(path)}", raw=True)

    def exec(self, sid, cmd, user="agent", timeout=60, env=None, cwd=None, landlock=None):
        body = {"cmd": cmd, "user": user, "timeout": timeout}
        if env: body["env"] = env
        if cwd: body["cwd"] = cwd
        if landlock is not None: body["landlock"] = landlock
        r = self._req("POST", f"/sandboxes/{sid}/exec", body, timeout=timeout + 60)
        return {"exit": r.get("exit", -1), "stdout": r.get("stdout", ""), "stderr": r.get("stderr", ""),
                "timeout": bool(r.get("timeout")), "elapsed_s": r.get("elapsed_s")}

    def events(self, sid):
        """Kernel ABI event log of the sandbox (agent-runtime); empty when the template has no ABI."""
        try: return self._req("GET", f"/sandboxes/{sid}/events?since=0&max=2000").get("events", [])
        except InfraError: return []

    def destroy(self, sid):
        try: self._req("DELETE", f"/sandboxes/{sid}")
        except InfraError: pass


class LocalBackend:
    """Same interface on local temp dirs + subprocess. A 'sandbox' is a directory; guest-absolute paths map under it."""
    users = False
    last_node = "local"

    def __init__(self, root_dir=None):
        self.root = root_dir or tempfile.mkdtemp(prefix="fc-eval-local-")
        os.makedirs(self.root, exist_ok=True)

    def path(self, sid, p):
        return os.path.join(self._dir(sid), p.lstrip("/"))

    def _dir(self, sid): return os.path.join(self.root, sid)

    def create(self, spec):
        sid = new_id("local")
        os.makedirs(self._dir(sid))
        return sid

    def put(self, sid, path, data):
        p = os.path.join(self._dir(sid), path.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f: f.write(data)

    def get(self, sid, path):
        p = os.path.join(self._dir(sid), path.lstrip("/"))
        if not os.path.exists(p): raise FileNotFoundError(p)
        return open(p, "rb").read()

    def events(self, sid): return []

    def exec(self, sid, cmd, user="agent", timeout=60, env=None, cwd=None, landlock=None):
        full = dict(os.environ, COPYFILE_DISABLE="1"); full.update(env or {})   # no AppleDouble files from macOS tar
        t0 = now()
        try:
            p = subprocess.run(["bash", "-c", cmd], cwd=cwd or self._dir(sid), env=full, capture_output=True,
                               timeout=timeout, text=True, errors="replace")
            return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "timeout": False, "elapsed_s": now() - t0}
        except subprocess.TimeoutExpired as e:
            return {"exit": 124, "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                    "stderr": "timeout", "timeout": True, "elapsed_s": now() - t0}

    def destroy(self, sid):
        shutil.rmtree(self._dir(sid), ignore_errors=True)


# ----------------------------------------------------------------------------------------------- run flow

SCORE_RE = re.compile(r"^\s*SCORE\s+([0-9]*\.?[0-9]+)\s*$")


def parse_score(exec_result):
    """Last line `SCORE x` of stdout -> float in [0,1]; nonzero exit, timeout or missing SCORE -> 0."""
    if exec_result["timeout"] or exec_result["exit"] != 0: return 0.0
    for line in reversed(exec_result["stdout"].splitlines()):
        if not line.strip(): continue
        m = SCORE_RE.match(line)
        return max(0.0, min(1.0, float(m.group(1)))) if m else 0.0
    return 0.0


def fill_cmd(template, **kw):
    """Substitute only the known {placeholders}; other braces (JSON, awk) stay literal."""
    for k, v in kw.items(): template = template.replace("{" + k + "}", str(v))
    return template


class ModelProxy:
    """Per-sandbox model tokens and usage (CONTRACT §4)."""
    def __init__(self, url, budget_usd):
        self.url, self.budget = url.rstrip("/"), budget_usd

    def _json(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise InfraError(f"model proxy {method} {path}: {e}") from None

    def token(self, sandbox):
        r = self._json("POST", "/tokens", {"sandbox": sandbox, "budget_usd": self.budget, "account": "eval"})
        return r.get("token") or r.get("key") or ""

    def usage(self, sandbox):
        try: return self._json("GET", f"/usage/{urllib.parse.quote(sandbox)}")
        except InfraError: return {}


class Runner:
    """Executes the agent phase and the verify phase of a run on a backend (survey §6.2)."""

    def __init__(self, backend, cfg, proxy=None):
        self.b, self.cfg, self.proxy = backend, cfg, proxy   # agent_cmd placeholders: {prompt} {out} {cwd} {task}

    def _spec(self, version, ttl, hosts):
        return {"class": "ephemeral", "template": version.get("template") or self.cfg["template"],
                "layers": version.get("layers") or self.cfg.get("layers") or [], "vcpu": 1, "mem": 512, "ttl_max_s": ttl + 180,
                "network": {"allow_hosts": hosts}}

    def _root(self, sid, cmd, timeout=120, what="setup"):
        r = self.b.exec(sid, cmd, user="root", timeout=timeout)
        if r["exit"] != 0: raise InfraError(f"{what} failed (exit {r['exit']}): {r['stderr'][-400:]}")
        return r

    def agent_phase(self, task, version, memory=True, extra="", ask="v1"):
        """Fresh sandbox with init/ at /task, run version.agent_cmd as agent, collect ws tar + traj. Destroys sandbox."""
        b = self.b
        hosts = [urllib.parse.urlparse(self.cfg["model_proxy"]).hostname] if self.cfg["model_proxy"] else []
        sid = b.create(self._spec(version, task.timeout_s, [h for h in hosts if h]))
        node = getattr(b, "last_node", None)
        P = lambda p: b.path(sid, p)  # noqa: E731
        try:
            b.put(sid, "/tmp/init.tar", tar_dir(task.init_dir))
            self._root(sid, f"mkdir -p {P('/task/.task')} && tar -C {P('/task')} -xf {P('/tmp/init.tar')}", what="init extract")
            prompt = task.prompt + (rules_prompt(task.rules) if memory else "") + (("\n\n" + ASK_RULES.get(ask, ASK_RULE) + " " + ASK_HOW) if task.blockers else "") + extra   # memory=False is the control arm: the same task without what the user taught
            b.put(sid, "/task/.task/prompt.md", prompt.encode())
            if b.users: self._root(sid, "chown -R agent:agent /task", what="chown")
            env = {"CAIRN_MODEL_NAME": self.cfg["model_name"]}
            if self.proxy:
                env["CAIRN_MODEL_URL"], env["CAIRN_MODEL_KEY"] = self.cfg["model_proxy"].rstrip("/") + "/v1", self.proxy.token(sid)
            cmd = fill_cmd(version.get("agent_cmd") or DEFAULT_AGENT_CMD, prompt=P("/task/.task/prompt.md"),
                           out=P("/task/.task/traj.json"), cwd=P("/task"), task=task.id)
            t0 = now()
            ar = b.exec(sid, cmd, user="agent", timeout=task.timeout_s, env=env, cwd=P("/task"), landlock=self.cfg.get("landlock", True) or None)
            elapsed = now() - t0
            events = b.events(sid)
            self._root(sid, f"tar -C {P('/task')} --exclude=.task -cf {P('/tmp/ws.tar')} .", what="workspace tar")
            ws = b.get(sid, "/tmp/ws.tar")
            try: traj = json.loads(b.get(sid, "/task/.task/traj.json"))
            except (InfraError, FileNotFoundError, ValueError): traj = None
            usage = self.proxy.usage(sid) if self.proxy else {}
            return {"ws": ws, "traj": traj, "agent_exit": ar["exit"], "agent_timeout": ar["timeout"], "events": events[-200:],
                    "agent_log": (ar["stdout"][-4000:] + "\n--- stderr ---\n" + ar["stderr"][-4000:]),
                    "elapsed_s": elapsed, "sandbox_id": sid, "node": node,
                    "cost": float(usage.get("cost_usd") or 0), "tokens_in": int(usage.get("tokens_in") or 0),
                    "tokens_out": int(usage.get("tokens_out") or 0), "tokens_cached": int(usage.get("tokens_cached") or 0)}
        finally:
            b.destroy(sid)

    def verify_phase(self, task, version, ws_tar):
        """Fresh no-network sandbox: /ws <- ws_tar, /verify <- task.verify_dir, run verify.sh. Destroys sandbox."""
        b = self.b
        sid = b.create(self._spec(version, VERIFY_TIMEOUT_S, []))
        P = lambda p: b.path(sid, p)  # noqa: E731
        try:
            b.put(sid, "/tmp/ws.tar", ws_tar)
            self._root(sid, f"mkdir -p {P('/ws')} && tar -C {P('/ws')} -xf {P('/tmp/ws.tar')}", what="ws extract")
            b.put(sid, "/tmp/verify.tar", tar_dir(task.verify_dir))
            self._root(sid, f"mkdir -p {P('/verify')} && tar -C {P('/verify')} -xf {P('/tmp/verify.tar')}", what="verify extract")
            t0 = now()
            r = b.exec(sid, f"bash {P('/verify/verify.sh')} {P('/ws')}", user="root", timeout=VERIFY_TIMEOUT_S)
            return {"exit": r["exit"], "stdout_tail": r["stdout"][-2000:], "stderr_tail": r["stderr"][-2000:],
                    "timeout": r["timeout"], "score": parse_score(r), "subtasks": parse_subtasks(r["stdout"]), "elapsed_s": now() - t0}
        finally:
            b.destroy(sid)

    def run_once(self, task, version, memory=True, ask="v1"):
        """One run. A task with blockers is a conversation: when the agent asks, a scripted person answers (a blocker's answer when the question is
        about it, otherwise "decide yourself") and the agent starts again with the exchange appended; the workspace that is verified is the last one."""
        asks, extra, used = [], "", set(); tot = {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0}
        try:
            for rnd in range(MAX_ASKS + 1):
                a = self.agent_phase(task, version, memory=memory, extra=extra, ask=ask)
                for k in tot: tot[k] += a.get(k) or 0
                q = question_of(a["traj"]) if task.blockers else None
                if not q: break
                bi = match_blocker(q, task.blockers, used) if rnd < MAX_ASKS else None
                asks.append({"q": q, "blocker": bi})
                if rnd == MAX_ASKS: break
                used.add(bi) if bi is not None else None
                extra += f"\n\nYou asked: {q}\nThe user answered: {task.blockers[bi]['answer'] if bi is not None else 'There is nothing more to tell you; use your best judgement.'}"
        except InfraError as e:
            e.cost, e.tokens_in, e.tokens_out, e.tokens_cached = tot["cost"], tot["tokens_in"], tot["tokens_out"], tot["tokens_cached"]; raise
        a.update(tot)
        try: v = self.verify_phase(task, version, a["ws"])
        except InfraError as e:                           # the agent phase was paid for; let the retry account for it
            e.cost, e.tokens_in, e.tokens_out, e.tokens_cached = a["cost"], a["tokens_in"], a["tokens_out"], a.get("tokens_cached", 0); raise
        raw = v["score"]; noop = (task.checks or {}).get("noop_subtasks"); v["score"] = lift_score(raw, v.get("subtasks"), noop); v["raw_score"] = raw
        v["lifted"] = bool(noop and v.get("subtasks") and all(f in v["subtasks"] for f in noop) and any(x < 1.0 - PASS_EPS for x in noop.values()))     # said on the run: a score equal to its raw value could be either
        init_tar = tar_dir(task.init_dir)
        cheat = cheat_signals(a["traj"], tar_listing(init_tar), tar_listing(a["ws"]), task.meta,
                              task.hidden_names(), len(a["ws"]), len(init_tar))
        return {"score": v["score"], "pass": is_pass(v["score"]), "agent_exit": a["agent_exit"], "events": a.get("events", []),
                "agent_timeout": a["agent_timeout"], "verify": {"exit": v["exit"], "stdout_tail": v["stdout_tail"],
                "score": v["score"], "raw_score": v["raw_score"], "lifted": v["lifted"], **({"subtasks": v["subtasks"]} if v.get("subtasks") else {}),
                **({"ask": {**ask_metrics(asks, task.blockers), "questions": [x["q"][:200] for x in asks]}} if task.blockers else {})}, "cheat": cheat, "traj": a["traj"], "ws": a["ws"], "agent_log": a["agent_log"],
                "elapsed_s": a["elapsed_s"], "sandbox_id": a["sandbox_id"], "node": a["node"], "cost": a["cost"],
                "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"], "tokens_cached": a.get("tokens_cached", 0)}

    def run_with_retry(self, task, version, retries=INFRA_RETRIES, memory=True, ask="v1"):
        """Retry InfraError up to `retries` attempts; returns (result or None, attempts, last_error). The model cost of an
        agent phase whose verify phase then failed is carried into the attempt that finally succeeds."""
        err = None; carried = {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0}
        for attempt in range(1, retries + 1):
            try:
                res = self.run_once(task, version, memory=memory, ask=ask)
                for k in carried: res[k] = res.get(k, 0) + carried[k]
                res["cost_carried"] = carried["cost"]
                return res, attempt, None
            except InfraError as e:
                err = str(e)
                for k in carried: carried[k] += getattr(e, k, 0) or 0
        lost = LostRun(err or ""); lost.carried = carried                    # the model calls of failed attempts were paid for: the row says so
        return None, retries, lost

    # ---- import checks (survey §6.7): only the verify half, no agent
    def import_check(self, task, version=None):
        version = version or {}
        with tempfile.TemporaryDirectory() as tmp:
            ref = os.path.join(tmp, "ref"); shutil.copytree(task.init_dir, ref)
            if os.path.isdir(task.reference_dir): shutil.copytree(task.reference_dir, ref, dirs_exist_ok=True)
            ref_tar, noop_tar = tar_dir(ref), tar_dir(task.init_dir)
        repeats, times = [], []
        for _ in range(3):
            v = self.verify_phase(task, version, ref_tar)
            repeats.append(v["score"]); times.append(v["elapsed_s"])
        noop = self.verify_phase(task, version, noop_tar)
        times.append(noop["elapsed_s"])
        checks = {"reference_pass": repeats[0] >= 1.0 - PASS_EPS, "noop_fail": noop["score"] < 1.0 - PASS_EPS,
                  "repeat_consistent": len(set(repeats)) == 1, "within_time": max(times) < VERIFY_TIMEOUT_S,
                  "scores": {"reference": repeats, "noop": noop["score"]}, "noop_subtasks": noop.get("subtasks") or {}, "verify_s": round(max(times), 3),
                  "noop_tail": noop["stdout_tail"][-300:], "checked_at": now()}
        os.makedirs(os.path.dirname(task.checks_path), exist_ok=True)
        with open(task.checks_path, "w") as f: json.dump(checks, f, indent=1)
        return checks


# ----------------------------------------------------------------------------------------------- cheat signals

GIT_HISTORY_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(log|show|reflog|checkout|switch|reset|restore|branch|stash)\b")


def traj_commands(traj):
    """Collect command-like strings from a mini-swe-agent-1.1 trajectory (assistant content, action/command fields)."""
    out = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("action", "command", "cmd") and isinstance(v, str): out.append(v)
                elif k == "content" and o.get("role") == "assistant" and isinstance(v, str): out.append(v)
                else: walk(v)
        elif isinstance(o, list):
            for x in o: walk(x)
    walk(traj or {})
    return out


def path_allowed(path, allowed):
    if any(a in (".", "./", "") for a in allowed): return True
    for a in allowed:
        if a.endswith("/") and (path + "/").startswith(a) or path == a.rstrip("/"): return True
    return False


def cheat_signals(traj, init_listing, ws_listing, meta, hidden_names, ws_bytes, init_bytes=0):
    sig = []
    for c in traj_commands(traj):
        if "/verify" in c or "verify.sh" in c: sig.append({"signal": "verify_mention", "detail": c[:200]})
        if ".git/" in c or GIT_HISTORY_RE.search(c): sig.append({"signal": "git_history", "detail": c[:200]})
    allowed = meta.get("allowed_write")
    changed = [p for p, h in ws_listing.items() if h is not None and init_listing.get(p) != h]
    changed += [p for p, h in init_listing.items() if h is not None and p not in ws_listing]   # deleted files
    if allowed is not None:
        bad = [p for p in changed if not path_allowed(p, allowed) and not IGNORED_WRITE_RE.search(p)]   # bytecode/test caches are noise, not signals
        if bad: sig.append({"signal": "write_outside_allowed", "detail": ",".join(sorted(bad)[:20])})
    col = sorted({p for p in ws_listing if os.path.basename(p) in hidden_names and p not in init_listing})
    if col: sig.append({"signal": "hidden_name_collision", "detail": ",".join(col[:20])})
    if ws_bytes > MAX_WS_BYTES + init_bytes: sig.append({"signal": "output_size", "detail": f"{ws_bytes} bytes (started at {init_bytes})"})     # growth beyond what the task began with: a task made from a real repository starts bigger than the limit
    return sig


# ----------------------------------------------------------------------------------------------- stats

def _valid(scores): return [s for s in scores if s is not None]


def per_task_stats(matrix):
    """matrix: {task: [score|None,...]} (None = infra_fail, excluded). -> {task: {p_hat, var, n, pass_all, pass_rate}}"""
    out = {}
    for t, scores in matrix.items():
        v = _valid(scores)
        if not v: continue
        out[t] = {"p_hat": statistics.fmean(v), "var": statistics.pvariance(v) if len(v) > 1 else 0.0, "n": len(v),
                  "pass_rate": statistics.fmean(1.0 if is_pass(s) else 0.0 for s in v),
                  "pass_all": all(is_pass(s) for s in v)}
    return out


def split_half_delta(matrix, n_rep=200, seed=0):
    """Noise band (docs/007): the 95th percentile of |mean(A) - mean(B)| where A and B are two independent k-run evaluations
    of the same version, simulated by resampling each task's k runs with replacement (two draws of k per task).
    The earlier half-split (1 vs 2 runs at k=3) overstated the band for the k actually used. Needs k>=2 valid runs per
    task; tasks with fewer are skipped; None when nothing qualifies."""
    rows = [list(_valid(s)) for s in matrix.values() if len(_valid(s)) >= 2]
    if not rows: return None
    rng = random.Random(seed); diffs = []
    for _ in range(n_rep):
        a_sum = b_sum = 0.0
        for r in rows:
            k = len(r)
            a_sum += sum(r[rng.randrange(k)] for _ in range(k)) / k; b_sum += sum(r[rng.randrange(k)] for _ in range(k)) / k
        diffs.append(abs(a_sum - b_sum) / len(rows))
    diffs.sort()
    return diffs[min(len(diffs) - 1, int(math.ceil(0.95 * len(diffs))) - 1)]


def binom_two_sided(b, c):
    """Exact two-sided binomial test of b vs c (p=0.5), i.e. McNemar on discordant pairs. n=0 -> 1.0."""
    n = b + c
    if n == 0: return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_stats(cand, base, n_boot=10000, seed=0):
    """Paired difference on common tasks: diff = mean(p_cand - p_base), bootstrap-over-tasks 95% CI,
    McNemar exact p when both sides have k==1 (else None)."""
    pc, pb = per_task_stats(cand), per_task_stats(base)
    common = sorted(set(pc) & set(pb))
    if not common: return {"diff": None, "ci95": [None, None], "mcnemar_p": None, "n_tasks": 0}
    d = [pc[t]["p_hat"] - pb[t]["p_hat"] for t in common]
    rng = random.Random(seed); n = len(d); boots = []
    for _ in range(n_boot):
        boots.append(sum(d[rng.randrange(n)] for _ in range(n)) / n)
    boots.sort()
    lo, hi = boots[int(0.025 * (n_boot - 1))], boots[int(0.975 * (n_boot - 1))]
    p = None
    if all(pc[t]["n"] == 1 and pb[t]["n"] == 1 for t in common):
        b = sum(1 for t in common if pc[t]["pass_all"] and not pb[t]["pass_all"])
        c = sum(1 for t in common if pb[t]["pass_all"] and not pc[t]["pass_all"])
        p = binom_two_sided(b, c)
    return {"diff": statistics.fmean(d), "ci95": [lo, hi], "mcnemar_p": p, "n_tasks": n}


def batch_stats(matrix, base_matrix=None, base_delta=None, seed=0):
    pt = per_task_stats(matrix)
    if not pt: return {"mean": None, "avg_k": None, "pass_at_1": None, "pass_k": None, "per_task": {}, "delta": None, "n_tasks": 0}
    mean = statistics.fmean(v["p_hat"] for v in pt.values())
    st = {"mean": mean, "avg_k": mean, "n_tasks": len(pt),
          "pass_at_1": statistics.fmean(v["pass_rate"] for v in pt.values()),
          "pass_k": statistics.fmean(1.0 if v["pass_all"] else 0.0 for v in pt.values()),
          "per_task": {t: {"p_hat": v["p_hat"], "var": v["var"], "n": v["n"], "pass_k": v["pass_all"]} for t, v in pt.items()},
          "delta": split_half_delta(matrix, seed=seed)}
    if base_matrix is not None:
        st["paired"] = paired_stats(matrix, base_matrix, seed=seed)
        st["accept"], st["reason"] = accept_decision(st["paired"], base_delta if base_delta is not None else st["delta"])
    return st


def accept_decision(paired, delta):
    d, (lo, _) = paired["diff"], paired["ci95"]
    if d is None: return False, "no common tasks with valid runs"
    dl = delta if delta is not None else 0.0
    if d <= dl: return False, f"diff {d:.4f} <= delta {dl:.4f} (within noise band{'' if delta is not None else ', delta unknown -> 0'})"
    if lo is None or lo <= 0: return False, f"diff {d:.4f} > delta {dl:.4f} but ci95 lower bound {lo:.4f} <= 0"
    return True, f"diff {d:.4f} > delta {dl:.4f} and ci95 [{lo:.4f}, {paired['ci95'][1]:.4f}] > 0"


# ----------------------------------------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, label TEXT, agent_cmd TEXT, layers TEXT, note TEXT, parent TEXT, runtime TEXT, harness_layer TEXT,
  template TEXT, created REAL);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, kind TEXT, taskset TEXT, version TEXT, k INTEGER, label TEXT,
  status TEXT, created REAL, params TEXT, progress TEXT, stats TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, batch TEXT, task TEXT, version TEXT, k_index INTEGER, stage TEXT,
  status TEXT, score REAL, pass INTEGER, infra_fail INTEGER, attempts INTEGER, cost REAL, tokens_in INTEGER,
  tokens_out INTEGER, elapsed_s REAL, agent_exit INTEGER, verify TEXT, cheat TEXT, events TEXT, sandbox_id TEXT, node TEXT,
  error TEXT, created REAL);
CREATE INDEX IF NOT EXISTS runs_batch ON runs(batch);
"""
JSON_COLS = {"layers", "params", "progress", "stats", "summary", "verify", "cheat", "events"}
MIGRATIONS = ["ALTER TABLE versions ADD COLUMN runtime TEXT", "ALTER TABLE versions ADD COLUMN harness_layer TEXT",   # ignored when already applied
              "UPDATE versions SET agent_cmd=NULL WHERE agent_cmd LIKE 'python3 -m aios.run%'",                           # pre-harness-layer default -> follow the current default
              "ALTER TABLE runs ADD COLUMN events TEXT", "ALTER TABLE runs ADD COLUMN tokens_cached INTEGER"]


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL"); self.db.executescript(SCHEMA)
            for m in MIGRATIONS:
                try: self.db.execute(m)
                except sqlite3.OperationalError: pass   # column already there

    @staticmethod
    def _enc(d): return {k: (json.dumps(v) if k in JSON_COLS else v) for k, v in d.items()}

    @staticmethod
    def _dec(row):
        if row is None: return None
        d = dict(row)
        for k in JSON_COLS:
            if k in d and isinstance(d[k], str): d[k] = json.loads(d[k])
        if "pass" in d: d["pass"] = bool(d["pass"]) if d["pass"] is not None else None
        if "infra_fail" in d: d["infra_fail"] = bool(d["infra_fail"])
        return d

    def insert(self, table, d):
        d = self._enc(d)
        with self.lock:
            self.db.execute(f"INSERT INTO {table}({','.join(d)}) VALUES({','.join('?' * len(d))})", list(d.values()))

    def update(self, table, rid, d):
        d = self._enc(d)
        with self.lock:
            self.db.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [rid])

    def get(self, table, rid):
        with self.lock: return self._dec(self.db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone())

    def all(self, table, where="", args=(), order="created"):
        with self.lock:
            return [self._dec(r) for r in self.db.execute(f"SELECT * FROM {table} {where} ORDER BY {order}", args)]


# ----------------------------------------------------------------------------------------------- evaluator

class Evaluator:
    """Owns the store, the backend, the run pool and batch threads."""

    def __init__(self, cfg, backend=None):
        global CHECKS_DIR
        self.cfg = cfg; CHECKS_DIR = os.path.join(cfg["state"], "checks")      # import-check results follow this instance's state dir
        os.makedirs(cfg["state"], exist_ok=True)
        self.store = Store(os.path.join(cfg["state"], "evaluator.sqlite"))
        self.backend = backend or CPBackend(cfg["cp_url"], cfg["cp_token"])
        self.proxy = ModelProxy(cfg["model_proxy"], cfg["budget_usd"]) if cfg["model_proxy"] else None
        self.runner = Runner(self.backend, cfg, self.proxy)
        self.pool = ThreadPoolExecutor(max_workers=cfg["max_parallel"])
        self.cancelled = set()
        self.lock = threading.Lock(); self._ts_lock = threading.Lock()

    # ---- versions / tasksets
    def add_version(self, body):
        vid = new_id("v")
        self.store.insert("versions", {"id": vid, "label": body.get("label", vid), "agent_cmd": body.get("agent_cmd") or None,   # None = current default at run time
                                       "runtime": body.get("runtime"), "harness_layer": body.get("harness_layer"),   # Cairn version = runtime hash + harness layer
                                       "layers": body.get("layers") or [], "note": body.get("note", ""), "parent": body.get("parent"),
                                       "template": body.get("template"), "created": now()})
        return vid

    def version(self, vid):
        v = self.store.get("versions", vid)
        if v is None: raise KeyError(f"unknown version {vid}")
        return v

    def taskset(self, name):
        try: return load_taskset(taskset_dirs(self.cfg), name)
        except (FileNotFoundError, ValueError): raise KeyError(f"unknown taskset {name}") from None

    # ---- batches
    def submit_batch(self, body):
        kind = body.get("kind", "adhoc")
        if kind not in ("baseline", "compare", "check", "adhoc"): raise ValueError("bad kind")
        ts = self.taskset(body["taskset"])
        version = {"id": None, "agent_cmd": DEFAULT_AGENT_CMD, "layers": []} if kind == "check" else self.version(body["version"])
        if kind == "compare" and not body.get("base_batch"): raise ValueError("compare needs base_batch")
        if body.get("base_batch") and self.store.get("batches", body["base_batch"]) is None: raise KeyError("unknown base_batch")
        bid = new_id("b")
        params = {"tasks": body.get("tasks"), "base_batch": body.get("base_batch"), "cascade": bool(body.get("cascade")), "smoke_k": max(1, min(5, int(body.get("smoke_k") or 1))),
                  "memory": "off" if body.get("memory") == "off" else "on", "ask": "v2" if body.get("ask") == "v2" else "v1", "taskset_version": ts["version"], "scoring": SCORING_VERSION}                  # off: dependent tasks run without the rules (the control arm of the memory lift)
        self.store.insert("batches", {"id": bid, "kind": kind, "taskset": ts["name"], "version": version["id"], "k": int(body.get("k", 5)),
                                      "label": body.get("label", ""), "status": "queued", "created": now(), "params": params,
                                      "progress": {"done": 0, "total": 0, "infra_fail": 0}, "stats": None, "summary": None})
        threading.Thread(target=self._guard, args=(bid,), daemon=True).start()
        return bid

    def _guard(self, bid):
        try: self.execute_batch(bid)
        except Exception as e:  # noqa: BLE001 - batch-level failure is recorded, never crashes the service
            self.store.update("batches", bid, {"status": "failed", "summary": {"error": f"{type(e).__name__}: {e}"}})

    def _tasks_for(self, batch, ts):
        sel = (batch.get("params") or {}).get("tasks")
        return [t for t in ts["tasks"] if not sel or t.id in sel]

    @staticmethod
    def scorable(tasks):
        """A task that failed its own check cannot be scored: if the no-op passes it hands out free points, if the reference fails nobody can win it.
        Unchecked tasks still run (a fresh import has not been through the check yet); invalid ones are left out and said so."""
        return [t for t in tasks if t.status != "invalid"], [t.id for t in tasks if t.status == "invalid"]

    def execute_batch(self, bid):
        batch = self.store.get("batches", bid)
        ts = self.taskset(batch["taskset"])
        tasks = self._tasks_for(batch, ts)
        self.store.update("batches", bid, {"status": "running"})
        if batch["kind"] == "check":
            return self._execute_check(bid, tasks)
        tasks, skipped = self.scorable(tasks)
        if skipped: self.store.update("batches", bid, {"params": {**batch["params"], "skipped_invalid": skipped}})
        if not tasks: raise ValueError(f"every task in {batch['taskset']} failed its check: {', '.join(skipped)}")
        version = self.version(batch["version"])
        base = self.store.get("batches", batch["params"].get("base_batch")) if batch["params"].get("base_batch") else None
        if batch["kind"] == "compare" and batch["params"].get("cascade"):
            smoke = [t for t in tasks if t.meta.get("smoke")]
            if smoke:
                sk = int(batch["params"].get("smoke_k") or 1)     # smoke_k > 1: a task eliminates the candidate only when every one of its runs fails
                self._run_stage(bid, smoke, version, sk, "smoke", total=len(smoke) * sk + len(tasks) * batch["k"])
                runs = self.store.all("runs", "WHERE batch=? AND stage=?", (bid, "smoke"))
                by_task = {}
                for r in runs: by_task.setdefault(r["task"], []).append(r)
                # a smoke run "fails" only at score 0: smoke catches broken candidates, and fractional verifiers (WorkBuddy) give
                # partial credit that is not a failure (0.75 on a task the seed also scores 0.75 on must not eliminate)
                failed = [t for t, rs in by_task.items() if rs and all(r["score"] is not None and float(r["score"]) <= 0 for r in rs)]
                if bid in self.cancelled: return self.store.update("batches", bid, {"status": "failed", "summary": {"error": "cancelled"}})
                if failed:
                    st = {"mean": None, "avg_k": None, "per_task": {}, "delta": None, "accept": False, "reason": "smoke: " + ", ".join(sorted(failed)),
                          "smoke": {t: [r["score"] for r in rs] for t, rs in by_task.items()}}
                    return self.store.update("batches", bid, {"status": "done", "stats": st, "summary": self._summary(bid, st)})
        self._run_stage(bid, tasks, version, batch["k"], "full")
        if bid in self.cancelled: return self.store.update("batches", bid, {"status": "failed", "summary": {"error": "cancelled"}})
        st = self._stats(bid, base)
        self.store.update("batches", bid, {"status": "done", "stats": st, "summary": self._summary(bid, st)})

    def _run_stage(self, bid, tasks, version, k, stage, total=None):
        runs = [(t, ki) for t in tasks for ki in range(k)]
        prog = self.store.get("batches", bid)["progress"]
        prog["total"] = total or (prog["total"] or len(runs))
        self.store.update("batches", bid, {"progress": prog})
        futs = [self.pool.submit(self._do_run, bid, t, version, ki, stage) for t, ki in runs]
        for f in futs: f.result()

    def _do_run(self, bid, task, version, k_index, stage):
        if bid in self.cancelled: return
        rid = new_id("r")
        row = {"id": rid, "batch": bid, "task": task.id, "version": version["id"], "k_index": k_index, "stage": stage,
               "status": "running", "created": now(), "infra_fail": 0, "attempts": 0}
        self.store.insert("runs", row)
        res, attempts, err = self.runner.run_with_retry(task, version, memory=((self.store.get("batches", bid).get("params") or {}).get("memory") != "off"), ask=((self.store.get("batches", bid).get("params") or {}).get("ask") or "v1"))
        rdir = os.path.join(self.cfg["state"], "runs", rid); os.makedirs(rdir, exist_ok=True)
        upd = {"status": "done", "attempts": attempts, "error": str(err) if err is not None else None}
        if res is None:
            c = getattr(err, "carried", None) or {}
            upd.update({"infra_fail": 1, "score": None, "pass": None, "cost": c.get("cost", 0.0), "tokens_in": c.get("tokens_in", 0), "tokens_out": c.get("tokens_out", 0), "tokens_cached": c.get("tokens_cached", 0)})
        else:
            upd.update({"score": res["score"], "pass": int(res["pass"]), "infra_fail": 0, "cost": res["cost"], "tokens_in": res["tokens_in"],
                        "tokens_out": res["tokens_out"], "tokens_cached": res.get("tokens_cached", 0), "elapsed_s": res["elapsed_s"], "agent_exit": res["agent_exit"], "events": res.get("events", []),
                        "verify": res["verify"], "cheat": res["cheat"], "sandbox_id": res["sandbox_id"], "node": res["node"]})
            with open(os.path.join(rdir, "ws.tar"), "wb") as f: f.write(res["ws"])
            with open(os.path.join(rdir, "agent.log"), "w") as f: f.write(res["agent_log"])
            if res["traj"] is not None:
                with open(os.path.join(rdir, "traj.json"), "w") as f: json.dump(res["traj"], f)
        self.store.update("runs", rid, upd)
        with self.lock:
            b = self.store.get("batches", bid); p = b["progress"]
            p["done"] += 1; p["infra_fail"] += int(res is None)
            self.store.update("batches", bid, {"progress": p})

    def matrix(self, bid, stage="full"):
        """{task: [{run_id, score, pass, infra_fail, cost, tokens_in, tokens_out, elapsed_s, cheat, k_index, stage}]}"""
        m = {}
        for r in self.store.all("runs", "WHERE batch=? AND status='done'" + (" AND stage=?" if stage else ""), (bid, stage) if stage else (bid,), "k_index"):
            m.setdefault(r["task"], []).append({"run_id": r["id"], "score": r["score"], "pass": r["pass"], "infra_fail": r["infra_fail"],
                                                "cost": r["cost"], "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "tokens_cached": r.get("tokens_cached") or 0,
                                                "elapsed_s": r["elapsed_s"], "cheat": r["cheat"] or [], "k_index": r["k_index"], "stage": r["stage"]})
        return m

    def _score_matrix(self, bid):
        return {t: [r["score"] for r in rs] for t, rs in self.matrix(bid).items()}

    def _stats(self, bid, base):
        bm = self._score_matrix(base["id"]) if base else None
        bd = (base.get("stats") or {}).get("delta") if base else None
        return batch_stats(self._score_matrix(bid), bm, bd)

    def _summary(self, bid, st):
        runs = self.store.all("runs", "WHERE batch=?", (bid,))
        return {"mean": st.get("mean"), "avg_k": st.get("avg_k"), "pass_at_1": st.get("pass_at_1"), "pass_k": st.get("pass_k"),
                "delta": st.get("delta"), "accept": st.get("accept"), "reason": st.get("reason"),
                "cost": sum(r["cost"] or 0 for r in runs), "tokens_in": sum(r["tokens_in"] or 0 for r in runs),
                "tokens_out": sum(r["tokens_out"] or 0 for r in runs), "tokens_cached": sum(r.get("tokens_cached") or 0 for r in runs), "infra_fail": sum(1 for r in runs if r["infra_fail"]),
                "cheat_signals": sum(len(r["cheat"] or []) for r in runs), **self._kinds_and_asks(bid, runs)}

    def _kinds_and_asks(self, bid, runs):
        """The score by kind of task (plain / dependent / obstacle / long) and the batch's Ask-F1, so an update can say where a version is better."""
        b = self.store.get("batches", bid)
        try: kinds = {t.id: t.kind for t in self.taskset(b["taskset"])["tasks"]}
        except KeyError: kinds = {}
        by = {}
        for r in runs:
            if r["score"] is None or r["infra_fail"]: continue
            by.setdefault(kinds.get(r["task"], "plain"), []).append(float(r["score"]))
        asks = [(r["verify"] or {}).get("ask") for r in runs if isinstance((r["verify"] or {}).get("ask"), dict)]
        def avg(xs): xs = [x for x in xs if x is not None]; return sum(xs) / len(xs) if xs else None
        out = {"by_kind": {k: {"mean": sum(v) / len(v), "n": len(v)} for k, v in by.items()}}
        if asks: out["ask"] = {"runs": len(asks), "precision": avg([a["precision"] for a in asks]), "recall": avg([a["recall"] for a in asks]), "f1": avg([a["f1"] for a in asks]), "asked": sum(a["asked"] for a in asks)}
        return out

    def _execute_check(self, bid, tasks):
        prog = {"done": 0, "total": len(tasks), "infra_fail": 0}
        self.store.update("batches", bid, {"progress": prog})
        results = {}

        def one(t):
            try: results[t.id] = self.runner.import_check(t)
            except Exception as e:  # noqa: BLE001 - one task's failure must not take the batch (or its sandboxes) down
                results[t.id] = {"error": f"{type(e).__name__}: {e}"}; prog["infra_fail"] += 1
            prog["done"] += 1; self.store.update("batches", bid, {"progress": prog})
        for f in [self.pool.submit(one, t) for t in tasks]: f.result()
        ok = all(all(r.get(k) for k in ("reference_pass", "noop_fail", "repeat_consistent", "within_time")) for r in results.values())
        self.store.update("batches", bid, {"status": "done", "summary": {"checks": results, "all_ok": ok}})
        return results

    def restat(self, bid):
        """Recompute stats for a finished batch from its stored runs (used after a change to the statistics)."""
        b = self.store.get("batches", bid)
        if b is None: raise KeyError(bid)
        if b["status"] != "done": raise ValueError("batch not done")
        base = self.store.get("batches", b["params"].get("base_batch")) if b["params"].get("base_batch") else None
        if base is not None and base.get("stats") is not None and base["id"] != bid: base["stats"] = batch_stats(self._score_matrix(base["id"]))
        if (b.get("stats") or {}).get("reason", "").startswith("smoke"): return b["stats"]       # eliminated at smoke: nothing to recompute
        st = self._stats(bid, base) if base is None else batch_stats(self._score_matrix(bid), self._score_matrix(base["id"]), (base.get("stats") or {}).get("delta"))
        self.store.update("batches", bid, {"stats": st, "summary": self._summary(bid, st)})
        if base is not None: self.store.update("batches", base["id"], {"stats": base["stats"]})
        return st

    def cancel(self, bid):
        self.cancelled.add(bid)
        b = self.store.get("batches", bid)
        if b and b["status"] in ("queued", "running"): self.store.update("batches", bid, {"status": "failed", "summary": {"error": "cancelled"}})
        return b is not None

    # ---- views (CONTRACT §3)
    def batch_view(self, bid, full=True):
        b = self.store.get("batches", bid)
        if b is None: return None
        v = {k: b[k] for k in ("id", "kind", "taskset", "version", "k", "label", "status", "created", "progress", "summary")}
        if full:
            runs = self.store.all("runs", "WHERE batch=?", (bid,))
            v.update({"runs": [r["id"] for r in runs], "matrix": self.matrix(bid, stage=None), "stats": b["stats"], "params": b["params"]})
        return v

    def run_view(self, rid):
        r = self.store.get("runs", rid)
        if r is None: return None
        p = os.path.join(self.cfg["state"], "runs", rid, "traj.json")
        r["traj"] = json.load(open(p)) if os.path.exists(p) else None
        return r

    def import_task(self, name, body):
        """A task mined from the user's work (docs/007): prompt, workspace at the base commit (tar.gz, top directory stripped),
        the PR's test files as hidden tests, the PR's source changes as the reference. Written under the state directory,
        then checked (reference passes, no-op fails) in its own batch."""
        tid = re.sub(r"[^A-Za-z0-9._-]", "-", str(body.get("id") or "")).lstrip(".")[:80]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name or "") or ".." in name: raise ValueError("bad taskset name")
        def safe(rel): return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/") and "\x00" not in rel          # every path a caller names stays inside the task directory
        tests = {k: v for k, v in (body.get("verify_files") or {}).items() if k.endswith(".py") and safe(k)}
        if not tid or ".." in tid or not body.get("prompt") or not body.get("init_tar_gz_b64") or not tests: raise ValueError("id, prompt, init_tar_gz_b64 and python verify_files required")
        base = os.path.join(self.cfg["state"], "tasksets", name); td = os.path.join(base, tid)
        import base64 as _b64, io as _io
        try: raw_tar = _b64.b64decode(body["init_tar_gz_b64"]); tarfile.open(fileobj=_io.BytesIO(raw_tar), mode="r:*").close()            # an archive that cannot be read must not cost the task its existing directory
        except Exception as e: raise ValueError(f"init_tar_gz_b64 is not a readable archive: {type(e).__name__}") from None
        shutil.rmtree(td, ignore_errors=True)
        try: os.remove(os.path.join(CHECKS_DIR, name, tid + ".json"))         # what was checked belonged to the old task: until the new check finishes the task is "unchecked", not "ok"
        except OSError: pass
        for sub in ("init", "verify", "reference"): os.makedirs(os.path.join(td, sub))
        with tarfile.open(fileobj=_io.BytesIO(raw_tar), mode="r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
            names = [re.sub(r"^(\./)+", "", m.name).rstrip("/") for m in members]
            firsts = {n.split("/")[0] for n in names if n and n != "."}
            top = next(iter(firsts)) if len(firsts) == 1 and any(n.startswith(next(iter(firsts)) + "/") for n in names) else None   # the one directory everything sits in (git archive --prefix, a host's tarball): the workspace is what is inside it
            for m, nm in zip(members, names):                                      # strip the archive's top directory; refuse paths that escape
                rel = nm.split("/", 1)[1] if top and nm.startswith(top + "/") else ("" if nm == top else nm)
                if not rel or rel.startswith("/") or ".." in rel.split("/"): continue
                if any(rel == x or rel.startswith(x + "/") for x in tests): continue  # the hidden tests are not in the workspace
                dest = os.path.join(td, "init", rel)
                if m.isdir(): os.makedirs(dest, exist_ok=True); continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with tf.extractfile(m) as src, open(dest, "wb") as out: shutil.copyfileobj(src, out)
                os.chmod(dest, (m.mode & 0o777) or 0o644)                       # the executable bit of a script survives
        for rel, content in tests.items():
            dest = os.path.join(td, "verify", rel); os.makedirs(os.path.dirname(dest), exist_ok=True); open(dest, "w").write(content)
        open(os.path.join(td, "verify", "tests.txt"), "w").write("\n".join(sorted(tests)) + "\n")
        open(os.path.join(td, "verify", "verify.sh"), "w").write(MINED_VERIFY_SH); os.chmod(os.path.join(td, "verify", "verify.sh"), 0o755)
        for rel, content in (body.get("reference_files") or {}).items():
            if not safe(rel): continue
            dest = os.path.join(td, "reference", rel); os.makedirs(os.path.dirname(dest), exist_ok=True); open(dest, "w").write(content)
        open(os.path.join(td, "prompt.md"), "w").write(body["prompt"])
        meta = {"id": tid, "domain": "coding", "group": body.get("group") or "evolve", "timeout_s": int(body.get("timeout_s") or 900), "allowed_write": ["./"], "smoke": False,
                "source": {"origin": "user", **(body.get("source") or {})}}
        if body.get("kind") in ("plain", "dependent", "obstacle", "long"): meta["kind"] = body["kind"]
        if body.get("rules"): meta["rules"] = [str(r)[:300] for r in body["rules"]][:30]
        if body.get("blockers"): meta["blockers"] = [{"question": str(b["question"])[:600], "answer": str(b["answer"])[:600]} for b in body["blockers"] if b.get("question") and b.get("answer")][:5]
        json.dump(meta, open(os.path.join(td, "meta.json"), "w"), indent=1, ensure_ascii=False)
        tsp = os.path.join(base, "taskset.json")
        with self._ts_lock:                                                    # two imports at once must not lose a task
            ts = json.load(open(tsp)) if os.path.exists(tsp) else {"name": name, "version": "0", "source": "mined from the user's own work (docs/007)", "tasks": []}
            if tid not in ts["tasks"]: ts["tasks"].append(tid)
            ts["version"] = taskset_digest(base, ts["tasks"])
            json.dump(ts, open(tsp, "w"), indent=1, ensure_ascii=False)
        bid = self.submit_batch({"kind": "check", "taskset": name, "tasks": [tid], "label": f"check mined task {tid}"})
        return {"task": tid, "taskset": name, "n_tasks": len(ts["tasks"]), "check_batch": bid}

    def taskset_view(self, name):
        ts = self.taskset(name)
        return {"name": ts["name"], "version": ts["version"], "tasks": [t.info() for t in ts["tasks"]]}

    def versions_tree(self):
        """Nodes = versions with their latest finished batch (mean, accept) and the evolve archive's verdict (docs/008 版本树)."""
        batches = [b for b in self.store.all("batches") if b.get("status") == "done" and b.get("kind") in ("baseline", "compare")]
        latest = {}
        for b in batches: latest[b["version"]] = b                                   # ordered by created: last wins
        ev_path = os.path.join(self.cfg["state"], "evolve.json")
        archive = {}
        if os.path.exists(ev_path):
            try: archive = {n["version"]: n for n in json.load(open(ev_path)).get("nodes", [])}
            except ValueError: archive = {}
        nodes = []
        for v in self.store.all("versions"):
            b = latest.get(v["id"]); st = (b or {}).get("stats") or {}; a = archive.get(v["id"], {})
            status = "superseded" if a.get("superseded") else ("stable" if a.get("note") == "seed" or a.get("accepted") else ("retired" if a.get("accepted") is False else "candidate"))
            nodes.append({"id": v["id"], "label": v["label"], "parent": v.get("parent"), "created": v["created"], "layers": v.get("layers") or [], "note": (v.get("note") or "")[:200],
                          "status": status, "mean": st.get("mean"), "accept": st.get("accept"), "reason": st.get("reason"), "batch": (b or {}).get("id"), "taskset": (b or {}).get("taskset"),
                          "archive": {k: a.get(k) for k in ("accepted", "note", "iteration") if k in a}})
        return {"nodes": nodes}

    def evolve_state(self):
        p = os.path.join(self.cfg["state"], "evolve.json")
        if not os.path.exists(p): return {"spent_usd": 0, "nodes": [], "iterations": []}
        d = json.load(open(p))
        for it in d.get("iterations", []): it.pop("diff", None)          # the full diff stays on disk; the panel shows the summary
        return d

    def health(self):
        cp = None
        if isinstance(self.backend, CPBackend):
            try: cp = self.backend._req("GET", "/health", timeout=5).get("ok", False)
            except InfraError: cp = False
        return {"ok": True, "cp": cp, "versions": len(self.store.all("versions")), "tasksets": [t["name"] for t in list_tasksets(taskset_dirs(self.cfg))]}


# ----------------------------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    ev: Evaluator = None
    protocol_version = "HTTP/1.1"
    ROUTES = [
        ("GET", r"/health", lambda ev, m, b: ev.health()),
        ("GET", r"/tasksets", lambda ev, m, b: {"tasksets": list_tasksets(taskset_dirs(ev.cfg))}),
        ("POST", r"/tasksets/([^/]+)/import", lambda ev, m, b: (201, ev.import_task(m[0], b))),
        ("GET", r"/tasksets/([^/]+)", lambda ev, m, b: ev.taskset_view(m[0])),
        ("POST", r"/tasksets/([^/]+)/check", lambda ev, m, b: (202, {"batch_id": ev.submit_batch({"kind": "check", "taskset": m[0], "tasks": b.get("tasks")})})),
        ("GET", r"/versions", lambda ev, m, b: {"versions": ev.store.all("versions")}),
        ("GET", r"/versions/tree", lambda ev, m, b: ev.versions_tree()),
        ("GET", r"/evolve", lambda ev, m, b: ev.evolve_state()),
        ("POST", r"/versions", lambda ev, m, b: (201, {"id": ev.add_version(b)})),
        ("GET", r"/batches", lambda ev, m, b: {"batches": [ev.batch_view(x["id"], full=False) for x in ev.store.all("batches")]}),
        ("POST", r"/batches", lambda ev, m, b: (202, {"batch_id": ev.submit_batch(b)})),
        ("GET", r"/batches/([^/]+)", lambda ev, m, b: ev.batch_view(m[0])),
        ("DELETE", r"/batches/([^/]+)", lambda ev, m, b: {"cancelled": ev.cancel(m[0])}),
        ("POST", r"/batches/([^/]+)/restat", lambda ev, m, b: {"stats": ev.restat(m[0])}),
        ("GET", r"/runs/([^/]+)", lambda ev, m, b: ev.run_view(m[0])),
    ]

    def log_message(self, *a): pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        tok = os.environ.get("CP_TOKEN", "") if os.environ.get("EVAL_REQUIRE_TOKEN") == "1" else ""     # docs/009: the control plane's bearer, once the scripts that call the evaluator bare are gone
        if tok and path != "/health" and not hmac.compare_digest(self.headers.get("Authorization") or "", f"Bearer {tok}"): return self._send(401, {"error": "bad token"})
        try: body = json.loads(raw) if raw else {}
        except ValueError: return self._send(400, {"error": "bad json"})
        for meth, pat, fn in self.ROUTES:
            m = re.fullmatch(pat, path)
            if meth == method and m:
                try:
                    r = fn(self.ev, m.groups(), body)
                    code, obj = r if isinstance(r, tuple) else (200, r)
                    return self._send(404, {"error": "not found"}) if obj is None else self._send(code, obj)
                except KeyError as e: return self._send(404, {"error": str(e)})
                except ValueError as e: return self._send(400, {"error": str(e)})
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "no route"})

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_DELETE(self): self._dispatch("DELETE")


def serve(ev, port):
    Handler.ev = ev
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"fc-eval listening on :{srv.server_address[1]} state={ev.cfg['state']} tasksets={ev.cfg['tasksets']}", flush=True)
    return srv


# ----------------------------------------------------------------------------------------------- CLI

def _wait(ev, bid, poll=0.5):
    while ev.store.get("batches", bid)["status"] in ("queued", "running"):
        time.sleep(poll)
    return ev.batch_view(bid)


def main(argv=None):
    ap = argparse.ArgumentParser(description="fc-eval evaluator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    c = sub.add_parser("check"); c.add_argument("taskset"); c.add_argument("--local", action="store_true"); c.add_argument("--tasks", nargs="*")
    r = sub.add_parser("run"); r.add_argument("taskset"); r.add_argument("--version"); r.add_argument("--k", type=int, default=1)
    r.add_argument("--local", action="store_true"); r.add_argument("--agent-cmd"); r.add_argument("--tasks", nargs="*")
    r.add_argument("--base", help="base batch id -> kind compare"); r.add_argument("--cascade", action="store_true")
    s = sub.add_parser("stats"); s.add_argument("batch_id")
    a = ap.parse_args(argv)
    cfg = cfg_from_env()
    local = getattr(a, "local", False)
    ev = Evaluator(cfg, LocalBackend() if local else None)
    if a.cmd == "serve":
        srv = serve(ev, cfg["port"])
        try: srv.serve_forever()
        except KeyboardInterrupt: pass
        return 0
    if a.cmd == "check":
        bid = ev.submit_batch({"kind": "check", "taskset": a.taskset, "tasks": a.tasks})
        v = _wait(ev, bid)
        for tid, c in v["summary"]["checks"].items():
            flags = " ".join(f"{k}={'ok' if c.get(k) else 'FAIL'}" for k in ("reference_pass", "noop_fail", "repeat_consistent", "within_time"))
            print(f"{tid:28s} {flags}  scores={c.get('scores')}" + (f"  error={c['error']}" if c.get("error") else ""))
        print("ALL OK" if v["summary"]["all_ok"] else "SOME CHECKS FAILED")
        return 0 if v["summary"]["all_ok"] else 1
    if a.cmd == "run":
        vid = a.version or ev.add_version({"label": "cli", "agent_cmd": a.agent_cmd, "layers": []})
        if a.agent_cmd and a.version: ev.store.update("versions", vid, {"agent_cmd": a.agent_cmd})
        body = {"kind": "compare" if a.base else "adhoc", "taskset": a.taskset, "version": vid, "k": a.k, "tasks": a.tasks,
                "base_batch": a.base, "cascade": a.cascade}
        bid = ev.submit_batch(body)
        v = _wait(ev, bid)
        print(f"batch {bid} status={v['status']} version={vid}")
        print_table(v); return 0
    if a.cmd == "stats":
        v = ev.batch_view(a.batch_id)
        if v is None: print("unknown batch"); return 1
        print_table(v); return 0


def print_table(v):
    print(f"{'task':28s} {'scores':24s} {'p_hat':>6s} {'var':>6s} {'infra':>5s} cheat")
    st = v.get("stats") or {}
    for t, rs in sorted(v["matrix"].items()):
        sc = " ".join("inf" if r["infra_fail"] else f"{r['score']:.2f}" for r in rs)
        pt = (st.get("per_task") or {}).get(t, {})
        print(f"{t:28s} {sc:24s} {pt.get('p_hat', float('nan')):6.3f} {pt.get('var', float('nan')):6.3f} "
              f"{sum(1 for r in rs if r['infra_fail']):5d} {sum(len(r['cheat']) for r in rs)}")
    print(f"mean(avg@k)={st.get('mean')} pass@1={st.get('pass_at_1')} pass^k={st.get('pass_k')} delta={st.get('delta')}")
    if "paired" in st: print(f"paired={st['paired']} accept={st.get('accept')} reason={st.get('reason')}")
    print(f"summary={v.get('summary')}")


if __name__ == "__main__":
    sys.exit(main())
