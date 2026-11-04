"""The names a task's hidden tests rely on that the workspace does not have yet (D24).

A mined task's prompt is a commit message, and the hidden tests are the original author's: they import `from pkg.mod import some_function` and call
`mod.other(...)`. The commit message never says what those are called, so a correct solution under another name fails -- which says nothing about the agent.
A real issue, or a pull request description, names them. This reads the tests' own imports and attribute uses with `ast`, no model, and lists what is missing:
only names, never code."""
import ast, builtins, os, sys

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)


def defined_names(src):
    """Top-level names a module source defines or re-exports."""
    try: tree = ast.parse(src)
    except (SyntaxError, ValueError): return None
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name): out.add(x.id)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name): out.add(n.target.id)
        elif isinstance(n, ast.Import): out.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom): out.update(a.asname or a.name for a in n.names)       # `from x import *` leaves "*" in the set: what it brings cannot be known here
        elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):               # names defined under `if`/`try`/`with`/`for` at module level still exist
            for x in ast.walk(n):
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): out.add(x.name)
                elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store): out.add(x.id)
                elif isinstance(x, ast.Import): out.update((a.asname or a.name).split(".")[0] for a in x.names)
                elif isinstance(x, ast.ImportFrom): out.update(a.asname or a.name for a in x.names)
    return out


def used_names(test_src):
    """{module: {names the test takes from it}} from `from m import a`, `import m` + `m.a`, `import m as x` + `x.a`. Relative imports are skipped."""
    try: tree = ast.parse(test_src)
    except (SyntaxError, ValueError): return {}
    out, alias = {}, {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            if n.module.split(".")[0] in STDLIB: continue
            out.setdefault(n.module, set()).update(a.name for a in n.names if a.name != "*")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] not in STDLIB: alias[a.asname or a.name] = a.name
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
            out.setdefault(alias[n.value.id], set()).add(n.attr)
    return out


def module_paths(module, paths):
    """Files that could be `module`: a/b/c.py or a/b/c/__init__.py, at any depth (a test adds its own directories to sys.path)."""
    tail = module.replace(".", "/")
    return [p for p in paths if p.endswith(".py") and (p == tail + ".py" or p.endswith("/" + tail + ".py") or p == tail + "/__init__.py" or p.endswith("/" + tail + "/__init__.py"))]


def missing_interfaces(tests, init_paths, read_init, added_sources, limit=12):
    """tests {path: source}; init_paths: files of the workspace; read_init(path) -> source; added_sources: Python source paths the original change added.
    -> lines like "new file `intake/commands.py`: `command`, `about_me`" and "`eval/evaluator.py` needs `lift_score`"."""
    wanted = {}
    for src in tests.values():
        for m, names in used_names(src).items(): wanted.setdefault(m, set()).update(names)
    lines = []
    for m in sorted(wanted):
        names = wanted[m] - set(dir(builtins))
        found = module_paths(m, init_paths)
        if found and found[0] in tests: continue                                       # a helper the commit itself rewrites ships with the hidden tests: it is not the agent's to write
        if found:
            have, parsed = set(), False
            for p in found:
                d = defined_names(read_init(p) or "")
                if d is not None: have |= d; parsed = True
            if not parsed or "*" in have: continue                                        # unreadable, or it re-exports with `import *`: not a reason to ask for every name
            siblings = {os.path.splitext(os.path.basename(q))[0] for q in init_paths if os.path.dirname(q) == os.path.dirname(found[0])} | \
                       {q.split("/")[-2] for q in init_paths if q.endswith("/__init__.py") and os.path.dirname(os.path.dirname(q)) == os.path.dirname(found[0])} | \
                       {os.path.splitext(os.path.basename(q))[0] for q in added_sources if os.path.dirname(q) == os.path.dirname(found[0])}      # a submodule the change adds is not a missing name of the package
            miss = sorted(n for n in names if n not in have and n not in siblings and not n.startswith("__"))      # a submodule of the package is a name it has
            if miss: lines.append(f"`{found[0]}` needs: " + ", ".join(f"`{n}`" for n in miss[:8]))
        else:
            new = module_paths(m, added_sources)
            if new: lines.append(f"new file `{new[0]}`" + (": " + ", ".join(f"`{n}`" for n in sorted(names)[:8]) if names else ""))
    return lines[:limit]
