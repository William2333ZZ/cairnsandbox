# code-interpreter: `run_code` of `e2b_code_interpreter`

The official `e2b_code_interpreter` SDK's `Sandbox.create()` + `run_code(...)` can be used directly against CairnSandbox: point `E2B_API_URL` and `E2B_SANDBOX_URL` at the control plane's E2B endpoint (see [api/e2b.md](api/e2b.md)). The SDK's default template is called `code-interpreter-v1`; below is how to build that template in your deployment.

```python
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create()                       # warm start from the template snapshot, measured at 0.2 s
print(sbx.run_code("x = 40\nx + 2").text)    # 42, a Python kernel whose state stays in the sandbox
```

## What it is and is not

E2B's official code-interpreter template runs a Jupyter kernel plus a FastAPI service (port 49999). Ours is **a small service we wrote ourselves** (`recipes/code-interpreter/server.py`, using only the Python standard library) that speaks the same HTTP protocol, so the SDK needs no changes.

| | Our service | E2B official template |
|---|---|---|
| `run_code`: result of the last expression, stdout / stderr separated, errors with name / value / traceback, `on_stdout` / `on_result` / `on_error` callbacks, streaming | Yes | Yes |
| Contexts: create, list, isolated state, `cwd`, restart, delete | Yes | Yes |
| Languages | python, bash, javascript (when bun or node is present) | python, javascript, r, java, bash, etc. |
| Rich output | Text, an object's `_repr_html_` / `_repr_markdown_` / `_repr_svg_` / `_repr_png_` / `_repr_json_` (DataFrame comes with html), **matplotlib figures** returned as PNG results (closed after use) | Full Jupyter rich output |
| Structured `chart` objects for charts, IPython magics, widgets | **No** (figures are PNG only) | Yes |
| Preinstalled libraries | Using the `ci-pylibs` layer: numpy, pandas, matplotlib, seaborn, scipy, scikit-learn, pillow, sympy, requests, httpx, beautifulsoup4, lxml, openpyxl, pyyaml, tqdm (Python 3.13); without this layer there is only the standard library and jinja2 | numpy, pandas, matplotlib, and many more |
| State | Python contexts are long-lived subprocesses, and variables persist between `run_code` calls; bash and javascript **run as a separate process each time, with no state** | Kernel state |

**Packages that are not there you must add yourself**: sandbox egress is closed by default, so `pip install` cannot reach the network. Add package names to `recipes/ci-pylibs/requirements.txt`, run `recipes/ci-pylibs/fetch-wheels.sh` on a machine with network access to fetch the wheels (only Linux x86_64, CPython 3.13 binary wheels are accepted; source-only packages cannot be installed), then rebuild the layer and the template. These packages are third-party and each has its own license (numpy, pandas, scipy, scikit-learn, etc. are BSD-style, matplotlib has its own permissive license, pillow is HPND); check them when you distribute a template that includes them.

## Building the template

This needs the agent-langs layer (CPython 3.13 plus bun). Its files (`cpython-*.tar.gz`, `bun`, `wheels/`) are downloaded on a machine with internet access and placed into `recipes/agent-langs/files/`; see `recipes/agent-langs/build.sh`.

```bash
# 1. Layers (on the machine where the node runs; layers and templates both live on the node that built them)
node/build-layer.sh recipes/agent-langs debian-13 "" http://127.0.0.1:18800        # prints the layer id, e.g. L-5f38d7a4
#    For numpy / pandas / matplotlib etc.: fetch wheels on a machine with network access, then stack the ci-pylibs layer on top of agent-langs
recipes/ci-pylibs/fetch-wheels.sh                                                      # about 110 MB, placed in recipes/ci-pylibs/files/ (gitignored)
BUILD_MEM=3072 node/build-layer.sh recipes/ci-pylibs debian-13 L-5f38d7a4 http://127.0.0.1:18800   # the third argument is the parent layer; e.g. L-6148aa3d
# 2. Template: start a sandbox with these layers, put in server.py, start it, verify one cell, then commit the "running sandbox" as a template (an existing template of the same name is replaced)
python3 recipes/code-interpreter/build-template.py --cp http://127.0.0.1:18700 --token "$(cat deploy/secrets/cp.token)" \
    --layer L-5f38d7a4 --layer L-6148aa3d --node fc-node --mem 2048
```

The template is a **memory plus disk** snapshot, so the service and its prewarmed Python kernel (numpy, pandas, matplotlib already imported) are already listening on 49999, and a new sandbox is usable the moment it comes up. For a template with these packages, the snapshot memory is about 243 MiB and the VM memory is 2048 MiB; the packed layer is about 1.4 GiB, and `BUILD_MEM` must be set generously during the build (3072). With multiple nodes, a template exists only on the node that built it, and scheduling prefers nodes that have the template; there is no ready-made "copy to all nodes" command (`POST /templates/everywhere` rebuilds from the spec; it does not copy).

## How the SDK reaches the service inside the sandbox

The SDK's requests to 49999 carry the `E2b-Sandbox-Id` and `E2b-Sandbox-Port: 49999` headers and go to `E2B_SANDBOX_URL` (no wildcard domain needed). When the control plane sees that the port is not envd's 49983, it forwards by port to the node's port proxy (streaming). Credentials: `/execute` carries `X-Access-Token`, while calls such as `/contexts` carry **only `E2B-Traffic-Access-Token`**, so besides `envdAccessToken` the create response also contains a **traffic token `trafficAccessToken` bound to this sandbox**; either token can open this sandbox's ports, a token from another sandbox cannot, and no token gives 401. The audit log has `op: envd_port` (port, method, path), and `/metrics` counts it under `cairn_envd_*`.

## Verification

`ops/e2b-code-interpreter-conformance.py` (17 cases, including numpy / pandas, matplotlib figures returned as PNG, and scipy / scikit-learn importing and running) was run against a 2-node drill stack on 2026-11-05: `e2b-code-interpreter` 2.10.5, 2.11.0, and 2.11.9 (paired with e2b 2.63.0) **all passed**. Measured over an SSH tunnel (including network round trips): create 0.17–0.23 s, first import of pandas + numpy + matplotlib 0.07–0.19 s (already preloaded), `describe` on a 100,000-row DataFrame 0.15 s, one matplotlib figure returned as PNG 0.21–0.28 s. Results are in [docs/research/e2b-conformance/](../../research/e2b-conformance/).

**Not verified**:
- Old versions 2.0–2.4: paired with the current e2b 2.63.0 they crash (accessing the removed private attribute `_transport`); this is an SDK problem, and we have no matching old e2b version to test with; 1.x does not recognize a custom API address, and the suite refuses to send requests to it.
- Structured `chart` objects, R, Java, IPython magics (see the table above, unsupported); third-party packages that are not there (you must add them yourself); large-data or long-running computations.
- Long-running or heavily concurrent `run_code`; template distribution across multiple nodes.
- The JS `@e2b/code-interpreter`.
