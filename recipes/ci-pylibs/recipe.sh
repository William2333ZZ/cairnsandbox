#!/bin/bash
# ci-pylibs layer: the Python packages code-interpreter users expect (numpy, pandas, matplotlib, scipy, scikit-learn, ...), installed into the agent-langs
# layer's CPython 3.13 (/aios/runtime/python). Build it ON TOP of agent-langs (parent layer). Offline: the wheels were downloaded elsewhere (fetch-wheels.sh)
# and are in /tmp/recipe-files/wheels; nothing is fetched from the network here. Runs as root in a build sandbox.
set -euo pipefail
F=/tmp/recipe-files; PY=/aios/runtime/python/bin/python3
test -x "$PY" || { echo "no $PY: build this layer on top of the agent-langs layer" >&2; exit 1; }
$PY -m pip install --quiet --no-index --find-links "$F/wheels" -r "$F/requirements.txt"
# smaller layer: no test suites, no bytecode caches (they are regenerated on first import)
find /aios/runtime/python/lib/python3.13/site-packages \( -name tests -o -name test -o -name __pycache__ \) -type d -prune -exec rm -rf {} + 2>/dev/null || true
chown -R root:root /aios && chmod -R a+rX /aios
cd /tmp && MPLBACKEND=Agg MPLCONFIGDIR=/tmp/mpl $PY - <<'PY'
import importlib
for m in ("numpy", "pandas", "matplotlib", "scipy", "sklearn", "PIL", "requests", "bs4", "lxml", "openpyxl", "yaml", "sympy", "seaborn", "tqdm", "httpx"):
    mod = importlib.import_module(m); print(f"{m:12} {getattr(mod, '__version__', '')}")
PY
du -sh /aios/runtime/python | sed 's/^/python tree: /'
