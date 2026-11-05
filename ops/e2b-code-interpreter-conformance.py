#!/usr/bin/env python3
"""The official `e2b_code_interpreter` SDK against a CairnSandbox stack that has the `code-interpreter-v1` template (recipes/code-interpreter, docs/guide/code-interpreter.md).

    pip install e2b-code-interpreter
    E2B_API_URL=http://127.0.0.1:18710 E2B_SANDBOX_URL=http://127.0.0.1:18710 E2B_API_KEY=<token> python3 ops/e2b-code-interpreter-conformance.py [--json out.json]

PASS / FAIL / SKIP per case like ops/e2b-conformance.py; the exit code is the number of FAILs. Refuses to run (sending nothing) if the SDK would talk to a different API URL.
"""
import argparse, importlib.metadata, json, os, sys, time

from e2b_code_interpreter import Sandbox

CASES = []
def case(name):
    def deco(fn): CASES.append((name, fn)); return fn
    return deco


class Skip(Exception): pass


@case("run_code: result of the last expression, stdout kept apart")
def _(sb):
    ex = sb.run_code("x = 40\nprint('hello')\nx + 2")
    assert ex.text == "42" and ex.logs.stdout == ["hello\n"] and ex.error is None, (ex.text, ex.logs, ex.error)


@case("state persists between cells of the default context")
def _(sb):
    sb.run_code("keep = 21")
    assert sb.run_code("keep * 2").text == "42"


@case("an exception becomes ExecutionError with name, value and traceback")
def _(sb):
    e = sb.run_code("1/0").error
    assert e and e.name == "ZeroDivisionError" and "division" in e.value and e.traceback, e


@case("stderr is reported separately")
def _(sb):
    assert sb.run_code("import sys; print('oops', file=sys.stderr)").logs.stderr == ["oops\n"]


@case("on_stdout callbacks arrive while the cell runs")
def _(sb):
    got, t0 = [], time.time()
    sb.run_code("import time\nfor i in range(3):\n    print(i, flush=True); time.sleep(0.5)", on_stdout=lambda m: got.append(time.time() - t0))
    assert len(got) == 3 and got[-1] - got[0] > 0.6, got


@case("on_result and on_error callbacks")
def _(sb):
    res, errs = [], []
    sb.run_code("5 + 5", on_result=lambda r: res.append(r.text)); sb.run_code("raise ValueError('x')", on_error=lambda e: errs.append(e.name))
    assert res == ["10"] and errs == ["ValueError"], (res, errs)


@case("rich result: an object with _repr_html_ carries html")
def _(sb):
    ex = sb.run_code("class T:\n    def _repr_html_(self): return '<b>hi</b>'\nT()")
    assert ex.results and ex.results[0].html == "<b>hi</b>", ex.results


@case("numpy and pandas work; a DataFrame carries an html result")
def _(sb):
    assert sb.run_code("import numpy as np\nint(np.arange(10).sum())").text == "45"
    ex = sb.run_code("import pandas as pd\npd.DataFrame({'a': [1, 2], 'b': [3, 4]})")
    assert ex.results and "<table" in (ex.results[0].html or ""), ex.results


@case("a matplotlib figure comes back as a PNG result")
def _(sb):
    import base64
    ex = sb.run_code("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [1, 4, 9])\nplt.title('squares')")
    pngs = [r.png for r in ex.results if r.png]
    assert pngs and base64.b64decode(pngs[0])[:8] == b"\x89PNG\r\n\x1a\n", ex.results
    assert not sb.run_code("import matplotlib.pyplot as plt\nlen(plt.get_fignums())").text == "1"      # the figure was closed after it was captured


@case("scipy, scikit-learn, pillow, sympy and the common parsers import and run")
def _(sb):
    ex = sb.run_code("import scipy, sklearn, PIL, sympy, openpyxl, bs4, lxml, yaml, requests, seaborn, tqdm, httpx\n"
                     "import numpy as np\nfrom sklearn.linear_model import LinearRegression\n"
                     "LinearRegression().fit(np.array([[1], [2], [3]]), np.array([2, 4, 6])).coef_.round(3).tolist()")
    assert ex.error is None and ex.text == "[2.0]", (ex.error, ex.text)


@case("contexts: create, isolated state, list, restart, remove")
def _(sb):
    ctx = sb.create_code_context()
    sb.run_code("y = 7", context=ctx)
    assert sb.run_code("y", context=ctx).text == "7"
    assert sb.run_code("y").error and sb.run_code("y").error.name == "NameError"
    assert ctx.id in [c.id for c in sb.list_code_contexts()]
    sb.restart_code_context(ctx)
    assert sb.run_code("y", context=ctx).error.name == "NameError"
    sb.remove_code_context(ctx)
    assert ctx.id not in [c.id for c in sb.list_code_contexts()]


@case("a context with a cwd")
def _(sb):
    ctx = sb.create_code_context(cwd="/tmp")
    try:
        assert sb.run_code("import os; os.getcwd()", context=ctx).text == "'/tmp'"
    finally:
        sb.remove_code_context(ctx)


@case("envs per run_code call")
def _(sb):
    assert sb.run_code("import os; os.environ['CI_VAR']", envs={"CI_VAR": "set"}).text == "'set'"
    assert sb.run_code("import os; os.environ.get('CI_VAR')").text is None   # restored after the call; None prints nothing, as in Jupyter


@case("bash and javascript languages")
def _(sb):
    assert sb.run_code("echo from-bash", language="bash").logs.stdout == ["from-bash\n"]
    assert sb.run_code("console.log(6 * 7)", language="javascript").logs.stdout == ["42\n"]


@case("an unsupported language is an error, not a hang")
def _(sb):
    try:
        sb.run_code("1", language="cobol")
    except Exception as e:
        assert "not supported" in str(e) or "400" in str(e), e
        return
    raise AssertionError("no error")


@case("the sandbox is a normal E2B sandbox too: commands and files work next to run_code")
def _(sb):
    sb.files.write("/tmp/ci-note.txt", "from files")
    assert sb.run_code("open('/tmp/ci-note.txt').read()").text == "'from files'"
    assert sb.commands.run("echo regular").stdout == "regular\n"


@case("the interpreter survives pause and connect (its kernel is in the VM's memory)")
def _(sb):
    sb.run_code("survivor = 'still here'")
    sb.pause()
    sb2 = Sandbox.connect(sb.sandbox_id)
    assert sb2.run_code("survivor").text == "'still here'"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--json"); a = ap.parse_args()
    for k in ("E2B_API_URL", "E2B_API_KEY"):
        if not os.environ.get(k): sys.exit(f"{k} is required")
    from e2b.connection_config import ConnectionConfig
    want, got = os.environ["E2B_API_URL"].rstrip("/"), ConnectionConfig().api_url.rstrip("/")
    if got != want: sys.exit(f"this SDK version would talk to {got}, not E2B_API_URL={want}: nothing was sent")
    sb = Sandbox.create(timeout=900); results = []
    try:
        for name, fn in CASES:
            t0 = time.time()
            try: fn(sb); st, why = "PASS", ""
            except Skip as e: st, why = "SKIP", str(e)
            except (AttributeError, ImportError) as e: st, why = "SKIP", f"SDK lacks it: {type(e).__name__}: {str(e)[:120]}"
            except TypeError as e: st, why = ("SKIP", f"SDK API differs: {str(e)[:120]}") if "unexpected keyword" in str(e) else ("FAIL", f"TypeError: {str(e)[:300]}")
            except Exception as e: st, why = "FAIL", f"{type(e).__name__}: {str(e)[:300]}"  # noqa: BLE001
            results.append({"case": name, "status": st, "why": why, "seconds": round(time.time() - t0, 2)})
            print(f"{st:5} {name}" + (f"  -- {why}" if why else ""), flush=True)
    finally:
        try: sb.kill()
        except Exception: pass  # noqa: BLE001
    n = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "SKIP")}
    ver = importlib.metadata.version("e2b-code-interpreter"); print(f"\ne2b-code-interpreter {ver} (e2b {importlib.metadata.version('e2b')}): {n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped, of {len(results)}")
    if a.json: json.dump({"e2b_code_interpreter_version": ver, "e2b_version": importlib.metadata.version("e2b"), "summary": n, "results": results}, open(a.json, "w"), indent=1)
    sys.exit(n["FAIL"])


if __name__ == "__main__":
    main()
