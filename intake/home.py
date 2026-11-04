# The person's harness directory on the host (docs/004 个人 harness 目录, D29): one per user under <state>/homes/<user>, shared by all their agents, and shipped into every
# sandbox at /work/home before the harness starts (CAIRN_HOME). The host reads the same files the runtime reads (aios/home.py: config.json, memory.md, rules/, prompts/,
# skills/, middleware/) and computes the same digest, so a ticket, an assignment and the trajectory all name one version of the directory. tasks/ and proposals/ never travel.
import hashlib, io, json, os, re, tarfile
from config import *

HOME_READ_DIRS = ("rules", "prompts", "skills", "middleware")
HOME_FILES = ("config.json", "memory.md")
HOME_IN_SANDBOX = "/work/home"
HOME_MAX_BYTES = 8 << 20                       # the whole directory; a harness directory is text
PATH_RE = re.compile(r"^(config\.json|memory\.md|(rules|prompts|skills|middleware)/[\w.\-]+(/[\w.\-]+)*)$")


def _ok_path(rel): return bool(rel and PATH_RE.fullmatch(rel) and not any(set(seg) <= {"."} for seg in rel.split("/")))     # no "." or ".." segments


def home_digest_of(root):
    """sha256 over (relative path, bytes) of the files the runtime reads, sorted: identical to aios.home.Home.digest()."""
    files = home_files_of(root); h = hashlib.sha256()
    for rel in files:
        with open(os.path.join(root, rel), "rb") as f: h.update(rel.encode() + b"\0" + f.read() + b"\0")
    return "h-" + h.hexdigest()[:10]


def home_files_of(root):
    out = [n for n in HOME_FILES if os.path.isfile(os.path.join(root, n))]
    for d in HOME_READ_DIRS:
        top = os.path.join(root, d)
        if not os.path.isdir(top): continue
        for dp, dns, fns in os.walk(top):
            dns[:] = [x for x in dns if x != "__pycache__"]
            for fn in fns: out.append(os.path.relpath(os.path.join(dp, fn), root))
    return sorted(out)


class HomeMixin:
    def home_dir(self):
        return os.path.join(self.cfg["state"], "homes", re.sub(r"[^\w.-]", "_", self.cfg.get("owner") or self.user))

    def home_files(self): return home_files_of(self.home_dir()) if os.path.isdir(self.home_dir()) else []

    def home_digest(self):
        """The digest of the directory as it is now, or "" when it has nothing the runtime would read (then nothing is shipped and the harness runs on the package alone)."""
        return home_digest_of(self.home_dir()) if self.home_files() else ""

    def home_tar(self):
        """The directory as a tar of the files the runtime reads (owner root, mode read-only), or None when there is nothing to ship."""
        files = self.home_files()
        if not files: return None
        root = self.home_dir(); buf = io.BytesIO(); total = 0
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for rel in files:
                p = os.path.join(root, rel); total += os.path.getsize(p)
                if total > HOME_MAX_BYTES: raise InfraError(f"harness directory is over {HOME_MAX_BYTES >> 20} MiB; not shipped")
                ti = tf.gettarinfo(p, arcname=rel); ti.uid = ti.gid = 0; ti.uname = ti.gname = "root"; ti.mode = 0o644
                with open(p, "rb") as f: tf.addfile(ti, f)
        return buf.getvalue()

    def home_volume(self):
        """Volume mode (INTAKE_HOME_VOLUME=1): one read-only volume per directory digest, `home-<digest>`, made once from the tar and attached to every
        sandbox; versions no sandbox holds are removed after each dispatch. Returns (volume id, digest), or (None, "") when there is nothing to ship."""
        digest = self.home_digest()
        if not digest: return None, ""
        name = f"home-{digest[2:]}"
        for v in self.cp.volumes():
            if v.get("name") == name: return v["id"], digest
        v = self.cp.volume_from_archive(name, self.home_tar(), size_mib=max(64, (len(self.home_tar()) >> 20) * 4 + 64))
        self.audit_home("volume_create", name, v.get("id"))
        return v["id"], digest

    def home_volume_gc(self, keep):
        """Drop older home volumes nothing holds (a 409 from the control plane means a sandbox still has it: leave it)."""
        for v in self.cp.volumes():
            if str(v.get("name", "")).startswith("home-") and v.get("id") != keep and not v.get("attached"):
                try: self.cp.volume_delete(v["id"])
                except Exception: pass  # noqa: BLE001

    def audit_home(self, op, name, vid):
        try: self.problems.pop("home_volume", None)
        except Exception: pass  # noqa: BLE001

    def home_ship(self, sid):
        """Put the directory into sandbox `sid` at /work/home, root-owned and read-only to the agent (the agent user may not edit its own rules; the machine's route is proposals/ on the host).
        Returns the digest shipped, or "" when nothing was."""
        tar = self.home_tar()
        if tar is None: return ""
        cp = self.cp; cp.put(sid, "/tmp/home.tar", tar)
        r = cp.exec(sid, f"rm -rf {HOME_IN_SANDBOX} && mkdir -p {HOME_IN_SANDBOX} && tar -C {HOME_IN_SANDBOX} -xf /tmp/home.tar && rm -f /tmp/home.tar && chown -R root:root {HOME_IN_SANDBOX} && chmod -R a+rX,go-w {HOME_IN_SANDBOX}",
                    user="root", timeout=60)
        if r["exit"] != 0: raise InfraError(f"harness directory not shipped: {r['stderr'][-300:]}")
        return self.home_digest()

    # --- the person's side: see and edit the files (the workbench), one file at a time; every edit is in effect at the next assignment ---
    def home_view(self):
        root = self.home_dir(); files = self.home_files()
        props = sorted(n for n in os.listdir(os.path.join(root, "proposals"))) if os.path.isdir(os.path.join(root, "proposals")) else []
        return {"dir": root, "digest": self.home_digest(), "in_sandbox": HOME_IN_SANDBOX, "files": [{"path": f, "size": os.path.getsize(os.path.join(root, f))} for f in files],
                "proposals": props, "problems": self.home_problems(), "limit_bytes": HOME_MAX_BYTES}

    def home_problems(self):
        """What the host can tell without the harness's code: the runtime's own check (aios.home) runs in the sandbox and stops the run; these catch the same things earlier."""
        root = self.home_dir(); bad = []
        p = os.path.join(root, "config.json")
        if os.path.isfile(p):
            try:
                cfg = json.load(open(p))
                if not isinstance(cfg, dict): bad.append("config.json must hold an object")
                else: bad += [f"config.json: unknown top-level key {k!r} (agent, environment, model)" for k in cfg if k not in ("agent", "environment", "model")]
            except ValueError as e: bad.append(f"config.json: {e}")
        p = os.path.join(root, "prompts", "instance.md")
        if os.path.isfile(p) and "{{task}}" not in open(p).read().replace(" ", ""): bad.append("prompts/instance.md must contain {{task}}")
        return bad

    def home_read(self, rel):
        if not _ok_path(rel): raise ValueError("not a file the runtime reads")
        p = os.path.join(self.home_dir(), rel)
        if not os.path.isfile(p): raise KeyError(rel)
        return {"path": rel, "text": open(p, encoding="utf-8").read(), "digest": self.home_digest()}

    def home_write(self, rel, text, actor="panel"):
        """Write one file. A person's edit is in effect at the next assignment: no version, no gate (D29). The machine never calls this."""
        if not _ok_path(rel): raise ValueError("path must be config.json, memory.md or a file under rules/, prompts/, skills/, middleware/")
        if not isinstance(text, str): raise ValueError("text must be a string")
        if len(text.encode()) > 1_000_000: raise ValueError("a file is at most 1 MB")
        if rel == "config.json": json.loads(text)                                   # refused before it is on disk; the harness would refuse to start on it
        p = os.path.join(self.home_dir(), rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"; open(tmp, "w", encoding="utf-8").write(text); os.replace(tmp, p)
        self._home_log("write", rel, actor); return self.home_view()

    def home_delete(self, rel, actor="panel"):
        if not _ok_path(rel): raise ValueError("not a file the runtime reads")
        p = os.path.join(self.home_dir(), rel)
        if not os.path.isfile(p): raise KeyError(rel)
        os.remove(p); self._home_log("delete", rel, actor); return self.home_view()

    def _home_log(self, op, rel, actor):
        with open(os.path.join(self.home_dir(), "history.jsonl"), "a") as f: f.write(json.dumps({"op": op, "path": rel, "by": actor, "at": now(), "digest": self.home_digest()}) + "\n")
