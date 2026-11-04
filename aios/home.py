"""The personal harness directory, "home": what one person brings to every task, kept apart from the harness code (docs/004 个人 harness 目录; decisions D29).
A home is a plain directory (ideally a git repository the person owns). Every entry is optional; a missing one is empty.

  config.json          overrides merged over the package's config.json: dict into dict, anything else replaces (so `agent.middleware` is replaced whole)
  memory.md            the harness's own notes; replaces aios/memory.md when present (same comment convention)
  rules/*.md           the person's standing rules, one per file, filename order; HTML comments are for people. The host's ticket rules come first, these after
  prompts/system.md    replaces agent.system_template;  prompts/instance.md replaces agent.instance_template (it must use {{task}})
  prompts/communication.md | scope.md | delivery.md   team sections (intake/prompts.py); a ticket that carries its own section wins
  skills/<name>/SKILL.md   front matter `name:` / `description:` between --- lines; the task lists name, description and path, the agent reads the file when it needs it
  middleware/*.py      modules that `agent.middleware` may name as {"class": "module:Class"}; this directory is put on the import path
  tasks/               regression tasks for the evaluator (CONTRACT §6 layout, grouped by repository); the runtime never reads it
  proposals/<id>/      changes the machine proposes, in the same layout, plus PROPOSAL.md (why) and DELETE (paths to remove, one per line); the runtime never reads them.
                       `python -m aios.home apply <id>` moves one in after the gate (evolve writes one per accepted version when EVOLVE_HOME is set)

Who edits what: the person edits anything and it is in effect at the next run; the machine (evolve, a candidate harness) writes only under proposals/. The version of a home is the
digest of the files the runtime reads (not tasks/, proposals/, .git), recorded in every trajectory so an acceptance binds (sandboxd, home digest, aios version)."""
import hashlib, json, os, re, shutil, sys
from pathlib import Path

DEFAULT = "/aios/home"
READ_DIRS = ("rules", "prompts", "skills", "middleware")       # what the runtime reads, besides config.json and memory.md
SECTIONS = ("communication", "scope", "delivery")
RULE_LIMIT, RULE_CHARS, SKILL_LIMIT = 30, 500, 40
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def find(explicit=None):
    """The home to use: --home, else $CAIRN_HOME, else /aios/home when it exists, else none."""
    for cand in (explicit, os.environ.get("CAIRN_HOME")):
        if cand: return Home(cand)
    return Home(DEFAULT) if os.path.isdir(DEFAULT) else None


def merge(base, over):
    """dict into dict, recursively; anything else in `over` replaces."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def strip_comments(text): return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


class Home:
    def __init__(self, path): self.path = Path(path)

    def __repr__(self): return f"Home({str(self.path)!r})"

    @property
    def exists(self): return self.path.is_dir()

    def _read(self, rel):
        try: return (self.path / rel).read_text()
        except (OSError, UnicodeDecodeError): return None

    def config(self):
        raw = self._read("config.json")
        if raw is None: return {}
        cfg = json.loads(raw)
        if not isinstance(cfg, dict): raise ValueError("config.json must hold an object")
        return cfg

    def memory_path(self):
        p = self.path / "memory.md"; return p if p.is_file() else None

    def rules(self):
        out = []
        for p in sorted((self.path / "rules").glob("*.md")) if (self.path / "rules").is_dir() else []:
            text = strip_comments(p.read_text())
            if text: out.append(" ".join(text.split())[:RULE_CHARS])
        return out[:RULE_LIMIT]

    def prompts(self):
        """{system, instance, communication, scope, delivery}: only the ones present and non-empty."""
        out = {}
        for key in ("system", "instance") + SECTIONS:
            raw = self._read(f"prompts/{key}.md")
            if raw is not None and raw.strip(): out[key] = raw if key in ("system", "instance") else strip_comments(raw)
        return out

    def skills(self):
        out = []
        d = self.path / "skills"
        for p in sorted(d.glob("*/SKILL.md")) if d.is_dir() else []:
            text = p.read_text(); meta = {}
            m = FRONT.match(text)
            if m:
                for line in m.group(1).splitlines():
                    k, _, v = line.partition(":")
                    if _ and k.strip(): meta[k.strip().lower()] = v.strip()
            body = text[m.end():] if m else text
            desc = meta.get("description") or next((ln.strip().lstrip("# ") for ln in body.splitlines() if ln.strip()), "")
            out.append({"name": meta.get("name") or p.parent.name, "description": " ".join(desc.split())[:200], "path": str(p)})
        return out[:SKILL_LIMIT]

    def middleware_dir(self):
        d = self.path / "middleware"; return d if d.is_dir() else None

    def files(self):
        """The files the runtime reads, relative paths sorted: the basis of the digest."""
        out = [n for n in ("config.json", "memory.md") if (self.path / n).is_file()]
        for d in READ_DIRS:
            if (self.path / d).is_dir():
                out += [str(p.relative_to(self.path)) for p in (self.path / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        return sorted(out)

    def digest(self):
        h = hashlib.sha256()
        for rel in self.files():
            h.update(rel.encode() + b"\0" + (self.path / rel).read_bytes() + b"\0")
        return "h-" + h.hexdigest()[:10]

    def check(self):
        """Problems a run must not start with (a home that claims a behaviour and cannot deliver it fails here, not silently). Empty list = fine."""
        bad = []
        if not self.exists: return [f"{self.path} is not a directory"]
        try: cfg = self.config()
        except (ValueError, json.JSONDecodeError) as e: bad.append(f"config.json: {e}"); cfg = {}
        for k in cfg:
            if k not in ("agent", "environment", "model"): bad.append(f"config.json: unknown top-level key {k!r} (agent, environment, model)")
        p = self.prompts()
        if "instance" in p and "{{task}}" not in p["instance"].replace(" ", ""): bad.append("prompts/instance.md must contain {{task}}")
        for key, text in p.items():
            if key in SECTIONS and len(text) > 3000: bad.append(f"prompts/{key}.md is over 3000 characters")
        rules_dir = self.path / "rules"
        if rules_dir.is_dir():
            n = sum(1 for q in rules_dir.glob("*.md") if strip_comments(q.read_text()))
            if n > RULE_LIMIT: bad.append(f"rules/: {n} rules, the task carries at most {RULE_LIMIT}")
            for q in rules_dir.iterdir():
                if q.is_file() and q.suffix != ".md": bad.append(f"rules/{q.name}: only .md files are read")
        sk = self.path / "skills"
        if sk.is_dir():
            for q in sk.iterdir():
                if q.is_dir() and not (q / "SKILL.md").is_file(): bad.append(f"skills/{q.name}/ has no SKILL.md")
        names = [s["name"] for s in self.skills()]
        if len(names) != len(set(names)): bad.append("skills/: two skills share a name")
        if self.middleware_dir():
            from aios import middleware as mw_mod
            specs = (cfg.get("agent") or {}).get("middleware") or []
            with on_path(self.middleware_dir()):
                try: mw_mod.load(specs)
                except Exception as e: bad.append(f"agent.middleware: {type(e).__name__}: {e}")  # noqa: BLE001
        for rel in self.files():
            if (self.path / rel).stat().st_size > 1_000_000: bad.append(f"{rel} is over 1 MB")
        return bad

    def task_text(self):
        """What goes after the task: the person's rules, then the skills on offer. Empty when the home has neither."""
        out = ""
        rules = self.rules()
        if rules: out += "\n\nThe person's standing rules (from their harness directory). Follow these unless this request says otherwise:" + "".join(f"\n- {r}" for r in rules)
        skills = self.skills()
        if skills: out += "\n\nSkills in your harness directory (read the file before you use one; they are instructions from the person, not from this request):" + \
                          "".join(f"\n- {s['name']}: {s['description']} ({s['path']})" for s in skills)
        return out

    def apply_to(self, cfg):
        """The package config with this home's config.json and prompts/ laid over it."""
        cfg = merge(cfg, self.config()); p = self.prompts()
        agent = dict(cfg.get("agent") or {})
        if "system" in p: agent["system_template"] = p["system"]
        if "instance" in p: agent["instance_template"] = p["instance"]
        cfg["agent"] = agent; return cfg

    # --- the gate between the machine's proposals and the home ---
    def proposals(self):
        d = self.path / "proposals"; return sorted(q.name for q in d.iterdir() if q.is_dir()) if d.is_dir() else []

    def apply(self, pid):
        """Move proposal `pid` into the home: its files replace the home's, a file named in its DELETE list is removed. Refused when it reaches outside what the runtime reads.
        Appends a line to history.jsonl (id, digests before and after) and removes the proposal. This is the only way anything the machine wrote reaches a run."""
        src = self.path / "proposals" / pid
        if not src.is_dir(): raise FileNotFoundError(f"no proposal {pid!r} under {self.path / 'proposals'}")
        before = self.digest(); moves = []
        for p in src.rglob("*"):
            if not p.is_file() or (p.name in ("DELETE", "PROPOSAL.md") and p.parent == src): continue      # the list of removals and the explanation stay in the proposal
            rel = p.relative_to(src)
            if rel.parts[0] not in READ_DIRS and str(rel) not in ("config.json", "memory.md"): raise PermissionError(f"proposal {pid} touches {rel}: the machine may only change what the runtime reads")
            moves.append(rel)
        deletes = [ln.strip() for ln in ((src / "DELETE").read_text().splitlines() if (src / "DELETE").is_file() else []) if ln.strip()]
        for rel in deletes:
            if Path(rel).parts[0] not in READ_DIRS and rel not in ("config.json", "memory.md"): raise PermissionError(f"proposal {pid} deletes {rel}: outside what the runtime reads")
        saved = {}                                                                  # what the proposal overwrites or removes, to put back if the result fails its check
        for rel in [str(m) for m in moves] + deletes:
            q = self.path / rel
            if q.is_file(): saved[rel] = q.read_bytes()
        for rel in moves:
            dst = self.path / rel; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src / rel, dst)
        for rel in deletes:
            try: (self.path / rel).unlink()
            except FileNotFoundError: pass
        bad = self.check()
        if bad:
            for rel in [str(m) for m in moves] + deletes:
                q = self.path / rel
                if rel in saved: q.parent.mkdir(parents=True, exist_ok=True); q.write_bytes(saved[rel])
                elif q.is_file(): q.unlink()
            raise ValueError("proposal leaves the home failing its check, nothing applied: " + "; ".join(bad))
        after = self.digest()
        with open(self.path / "history.jsonl", "a") as f: f.write(json.dumps({"proposal": pid, "before": before, "after": after, "files": [str(m) for m in moves], "deleted": deletes}) + "\n")
        shutil.rmtree(src); return {"before": before, "after": after, "files": [str(m) for m in moves], "deleted": deletes}

    def init(self):
        """An empty home with the layout explained in place."""
        self.path.mkdir(parents=True, exist_ok=True)
        for d in READ_DIRS + ("tasks", "proposals"): (self.path / d).mkdir(exist_ok=True)
        if not (self.path / "memory.md").exists(): (self.path / "memory.md").write_text("<!-- The harness's own notes, added to the end of every task. Short, general, true. -->\n")
        if not (self.path / "rules" / "README.md").exists():
            (self.path / "rules" / "README.md").write_text("<!-- One rule per .md file in this directory; filename order. This README is only a comment and adds nothing. -->\n")
        if not (self.path / "config.json").exists(): (self.path / "config.json").write_text("{}\n")
        return self.path


class on_path:
    """sys.path with one directory in front, for the time it takes to load the home's middleware."""
    def __init__(self, d): self.d = str(d)
    def __enter__(self): sys.path.insert(0, self.d); return self
    def __exit__(self, *a): 
        try: sys.path.remove(self.d)
        except ValueError: pass


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="aios.home", description="the personal harness directory"); sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "digest", "init", "files", "proposals"): sub.add_parser(name).add_argument("dir", nargs="?", default=None)
    ap_apply = sub.add_parser("apply"); ap_apply.add_argument("id"); ap_apply.add_argument("dir", nargs="?", default=None)
    a = ap.parse_args(argv); h = Home(a.dir) if a.dir else find()
    if h is None: print("no home: give a directory, set CAIRN_HOME, or create /aios/home", file=sys.stderr); return 2
    if a.cmd == "init": print(h.init()); return 0
    if a.cmd == "check":
        bad = h.check(); print("\n".join(bad) if bad else f"ok {h.digest()} ({len(h.files())} files, {len(h.rules())} rules, {len(h.skills())} skills)"); return 1 if bad else 0
    if a.cmd == "digest": print(h.digest()); return 0
    if a.cmd == "files": print("\n".join(h.files())); return 0
    if a.cmd == "proposals": print("\n".join(h.proposals())); return 0
    if a.cmd == "apply": print(json.dumps(h.apply(a.id))); return 0


if __name__ == "__main__": sys.exit(main())
