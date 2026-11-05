#!/usr/bin/env python3
"""A small code-interpreter server for Cairn sandboxes: speaks the protocol the `e2b_code_interpreter` SDKs use against `code-interpreter-v1`'s Jupyter-based
server (port 49999), with stateful Python contexts, plus bash and JavaScript. Python standard library only; started inside the template's sandbox and committed as
the template (docs/guide/code-interpreter.md), so a sandbox started from that template already has it listening.

  GET    /health
  POST   /contexts            {"language"?, "cwd"?, "name"?}     -> {"id", "language", "cwd"}
  GET    /contexts                                               -> [{"id", "language", "cwd"}, ...]
  DELETE /contexts/<id>
  POST   /contexts/<id>/restart
  POST   /execute             {"code", "context_id"?, "language"?, "env_vars"?}  -> NDJSON, one event per line:
         {"type":"stdout"|"stderr","text","timestamp"} | {"type":"result","text","html"?,"is_main_result",...} | {"type":"error","name","value","traceback"} |
         {"type":"number_of_executions","execution_count"}

Not Jupyter: no rich outputs beyond text and `_repr_html_`/`_repr_png_`/`_repr_svg_`/`_repr_markdown_`/`_repr_json_`, no charts, no widgets, languages are python, bash and
javascript (bun or node when present). What it is for: the common `run_code` loop of an LLM app.
"""
import ast, base64, json, os, shutil, subprocess, sys, threading, time, traceback, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CI_PORT", "49999"))
DEFAULT_CWD = os.environ.get("CI_CWD", os.path.expanduser("~") or "/")
LANGS = {"python": "python", "py": "python", "bash": "bash", "sh": "bash", "javascript": "javascript", "js": "javascript", "typescript": "javascript", "ts": "javascript"}


def now_ns(): return time.time_ns()


# ------------------------------------------------------------------ the Python kernel (a child process: restartable, isolated from the server)
def py_kernel_main():
    out = os.fdopen(os.dup(1), "w", buffering=1); os.dup2(2, 1)            # events go to the saved stdout; user print() goes through our stream objects
    lock = threading.Lock()

    def emit(ev):
        with lock: out.write(json.dumps(ev) + "\n"); out.flush()

    class Stream:
        """Line-buffered like a terminal: `print("a")` is one event "a\\n", a partial line leaves at flush() or at the end of the cell."""
        def __init__(self, kind): self.kind, self.buf = kind, ""
        def write(self, s):
            if not s: return 0
            self.buf += s
            if "\n" in self.buf:
                head, _, self.buf = self.buf.rpartition("\n"); emit({"type": self.kind, "text": head + "\n", "timestamp": now_ns()})
            return len(s)
        def flush(self):
            if self.buf: t, self.buf = self.buf, ""; emit({"type": self.kind, "text": t, "timestamp": now_ns()})
        def isatty(self): return False
        encoding = "utf-8"

    sys.stdout, sys.stderr = Stream("stdout"), Stream("stderr")
    ns = {"__name__": "__main__", "__builtins__": __builtins__}

    def rich(v, main):
        r = {"is_main_result": main}
        try: r["text"] = repr(v)
        except Exception as e: r["text"] = f"<unprintable {type(v).__name__}: {e}>"
        for attr, key in (("_repr_html_", "html"), ("_repr_markdown_", "markdown"), ("_repr_svg_", "svg"), ("_repr_latex_", "latex"), ("_repr_json_", "json")):
            f = getattr(v, attr, None)
            if callable(f):
                try:
                    x = f()
                    if x is not None: r[key] = x
                except Exception: pass
        f = getattr(v, "_repr_png_", None)
        if callable(f):
            try:
                x = f()
                if x: r["png"] = base64.b64encode(x).decode() if isinstance(x, (bytes, bytearray)) else x
            except Exception: pass
        return {"type": "result", **r}

    def display(*objs):
        for o in objs: emit(rich(o, False))
    ns["display"] = display
    count = 0
    for line in sys.stdin:
        req = json.loads(line); code, env = req.get("code", ""), req.get("env") or {}
        count += 1; saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            tree = ast.parse(code, "<cell>", "exec")
            last = None
            if tree.body and isinstance(tree.body[-1], ast.Expr): last = ast.Expression(tree.body.pop().value)
            exec(compile(tree, "<cell>", "exec"), ns)
            if last is not None:
                v = eval(compile(last, "<cell>", "eval"), ns)
                if v is not None: ns["_"] = v; emit(rich(v, True))
        except SystemExit as e:
            emit({"type": "error", "name": "SystemExit", "value": str(e.code), "traceback": ""})
        except BaseException as e:   # noqa: BLE001: a cell may raise anything; the kernel must survive it
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            emit({"type": "error", "name": type(e).__name__, "value": str(e), "traceback": tb})
        finally:
            sys.stdout.flush(); sys.stderr.flush()
            for k, v in saved.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
        emit({"type": "number_of_executions", "execution_count": count}); emit({"type": "done"})


# ------------------------------------------------------------------ contexts
class Context:
    def __init__(self, language, cwd, name=None):
        self.id, self.language, self.cwd, self.name = (name or uuid.uuid4().hex[:12]), language, cwd, name
        self.lock = threading.Lock(); self.count = 0; self.proc = None
        if language == "python": self.start()

    def info(self): return {"id": self.id, "language": self.language, "cwd": self.cwd}

    def start(self):
        self.stop()
        self.proc = subprocess.Popen([sys.executable, "-u", os.path.abspath(__file__), "--py-kernel"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=self.cwd if os.path.isdir(self.cwd) else None, text=True, bufsize=1, start_new_session=True)
        self.count = 0

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try: os.killpg(self.proc.pid, 9)
            except OSError: pass
            self.proc.wait()
        self.proc = None

    def run(self, code, env, emit):
        """Execute one cell; emit() gets each event dict as it happens."""
        with self.lock:
            {"python": self._py, "bash": self._bash, "javascript": self._js}[self.language](code, env, emit)

    def _py(self, code, env, emit):
        if not self.proc or self.proc.poll() is not None: self.start()
        self.proc.stdin.write(json.dumps({"code": code, "env": env}) + "\n"); self.proc.stdin.flush()
        for line in self.proc.stdout:
            ev = json.loads(line)
            if ev["type"] == "done": return
            emit(ev)
        emit({"type": "error", "name": "KernelDied", "value": "the Python kernel exited; the context was restarted, its variables are gone", "traceback": ""}); self.start()

    def _stream_proc(self, argv, env, emit, cwd):
        full = dict(os.environ); full.update({k: str(v) for k, v in env.items()})
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd if os.path.isdir(cwd) else None, env=full, start_new_session=True)
        def pump(f, kind):
            for chunk in iter(lambda: f.readline(), b""): emit({"type": kind, "text": chunk.decode("utf-8", "replace"), "timestamp": now_ns()})
        ts = [threading.Thread(target=pump, args=(p.stdout, "stdout")), threading.Thread(target=pump, args=(p.stderr, "stderr"))]
        [t.start() for t in ts]; code = p.wait(); [t.join() for t in ts]
        if code != 0: emit({"type": "error", "name": "ExitError", "value": f"exit status {code}", "traceback": ""})

    def _bash(self, code, env, emit): self.count += 1; self._stream_proc(["bash", "-c", code], env, emit, self.cwd); emit({"type": "number_of_executions", "execution_count": self.count})

    def _js(self, code, env, emit):
        rt = shutil.which("bun") or ("/aios/runtime/bun/bun" if os.path.exists("/aios/runtime/bun/bun") else None) or shutil.which("node")
        if not rt: emit({"type": "error", "name": "RuntimeMissing", "value": "no bun or node in this sandbox", "traceback": ""}); return
        self.count += 1; path = f"/tmp/ci-{uuid.uuid4().hex[:8]}.{'ts' if 'bun' in rt else 'js'}"
        open(path, "w").write(code)
        try: self._stream_proc([rt, "run", path] if "bun" in rt else [rt, path], env, emit, self.cwd)
        finally: os.unlink(path)
        emit({"type": "number_of_executions", "execution_count": self.count})


CONTEXTS = {}                       # id -> Context
DEFAULTS = {}                       # language -> Context (used when a request names no context)
REG = threading.Lock()


def get_default(lang):
    with REG:
        if lang not in DEFAULTS:
            c = Context(lang, DEFAULT_CWD); DEFAULTS[lang] = c; CONTEXTS[c.id] = c
        return DEFAULTS[lang]


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _text(self, code, msg):
        b = msg.encode(); self.send_response(code); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n) if n else b""
        try: return json.loads(raw) if raw else {}
        except ValueError: return None

    def do_GET(self):
        if self.path == "/health": return self._text(200, "OK")
        if self.path == "/contexts":
            with REG: return self._json(200, [c.info() for c in CONTEXTS.values()])
        self._text(404, "not found")

    def do_DELETE(self):
        cid = self.path.rsplit("/", 1)[-1]
        with REG:
            c = CONTEXTS.pop(cid, None)
            for k in [k for k, v in DEFAULTS.items() if v is c]: DEFAULTS.pop(k)
        if not c: return self._text(404, f"context {cid} not found")
        c.stop(); self._json(200, {})

    def do_POST(self):
        b = self._body()
        if b is None: return self._text(400, "bad JSON")
        if self.path == "/contexts":
            lang = LANGS.get((b.get("language") or "python").lower())
            if not lang: return self._text(400, f"language {b.get('language')!r} is not supported (python, bash, javascript)")
            c = Context(lang, b.get("cwd") or DEFAULT_CWD, b.get("name"))
            with REG: CONTEXTS[c.id] = c
            return self._json(200, c.info())
        if self.path.startswith("/contexts/") and self.path.endswith("/restart"):
            c = CONTEXTS.get(self.path.split("/")[2])
            if not c: return self._text(404, "context not found")
            with c.lock: c.start() if c.language == "python" else None
            return self._json(200, {})
        if self.path == "/execute": return self._execute(b)
        self._text(404, "not found")

    def _execute(self, b):
        code = b.get("code")
        if not isinstance(code, str): return self._text(400, "code is required")
        if b.get("context_id"):
            c = CONTEXTS.get(b["context_id"])
            if not c: return self._text(404, f"context {b['context_id']} not found")
        else:
            lang = LANGS.get((b.get("language") or "python").lower())
            if not lang: return self._text(400, f"language {b.get('language')!r} is not supported (python, bash, javascript)")
            c = get_default(lang)
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        wl = threading.Lock(); dead = []

        def emit(ev):
            if dead: return
            d = (json.dumps(ev) + "\n").encode()
            try:
                with wl: self.wfile.write(f"{len(d):x}\r\n".encode() + d + b"\r\n"); self.wfile.flush()
            except OSError: dead.append(1)
        try: c.run(code, b.get("env_vars") or {}, emit)
        except Exception as e: emit({"type": "error", "name": type(e).__name__, "value": str(e), "traceback": traceback.format_exc()})   # noqa: BLE001
        try: self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        except OSError: pass


def main():
    ThreadingHTTPServer.daemon_threads = True; ThreadingHTTPServer.request_queue_size = 64
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H); get_default("python")      # warm the default Python kernel so the first run_code is fast
    print(f"code-interpreter listening on :{PORT}", flush=True); srv.serve_forever()


if __name__ == "__main__":
    if "--py-kernel" in sys.argv: py_kernel_main()
    else: main()
