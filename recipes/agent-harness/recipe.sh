#!/bin/bash
# agent-harness layer: harness code only (the interpreters come from the agent-langs layer below it).
#   /aios/harness/aios     the Python seed harness (default)
#   /aios/bin/aios         launcher: `aios run ...` runs the default (Python) harness with /aios/runtime/python
#   /aios/bin/aios-ts      launcher for a TypeScript harness under /aios/harness/ts (bun); placeholder until one exists
set -euo pipefail
F=/tmp/recipe-files
mkdir -p /aios/harness /aios/bin
rm -rf /aios/harness/aios && cp -R "$F"/aios /aios/harness/aios && rm -rf /aios/harness/aios/__pycache__
cat > /aios/bin/aios <<'SH'
#!/bin/sh
# default harness = Python seed. usage: aios run --task-file ... --out ...
# -P: never put the working directory on sys.path. The repository under /work/repo may itself contain an `aios` package
# (this platform's own source), and the harness must run the layer's code, not the checkout's. Only the flag, not
# PYTHONSAFEPATH in the environment: that would leak into every shell the agent runs and break `python -c "import sibling"`
# in the task's own directory (the first thing the meta agent tried to fix, twice).
export PYTHONPATH=/aios/harness${PYTHONPATH:+:$PYTHONPATH} PYTHONDONTWRITEBYTECODE=1
unset PYTHONSAFEPATH
sub=${1:-run}; shift || true
exec /aios/runtime/python/bin/python3 -P -m "aios.$sub" "$@"
SH
cat > /aios/bin/aios-ts <<'SH'
#!/bin/sh
# TypeScript harness entry (bun). usage: aios-ts run ...  -> /aios/harness/ts/<sub>.ts
sub=${1:-run}; shift || true
[ -f "/aios/harness/ts/$sub.ts" ] || { echo "no TypeScript harness at /aios/harness/ts/$sub.ts" >&2; exit 1; }
exec /aios/runtime/bun/bun run "/aios/harness/ts/$sub.ts" "$@"
SH
chmod 0755 /aios/bin/aios /aios/bin/aios-ts; chown -R root:root /aios/harness /aios/bin; chmod -R a+rX /aios/harness /aios/bin
cd /tmp && /aios/bin/aios run --help >/dev/null && echo "selfcheck ok: python harness launches on the langs layer"
