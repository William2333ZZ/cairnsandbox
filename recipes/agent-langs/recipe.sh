#!/bin/bash
# agent-langs layer: the interpreters every Cairn harness may use, under /aios/runtime (never /usr).
#   /aios/runtime/python   relocatable CPython 3.13 (python-build-standalone) + jinja2 (harness dep)
#   /aios/runtime/bun      bun single binary (TypeScript/JavaScript)
# Shared by all harness layers; the harness layer holds code only. Runs as root in a build sandbox.
set -euo pipefail
F=/tmp/recipe-files
mkdir -p /aios/runtime /aios/bin
tar -C /aios/runtime -xzf "$F"/cpython-*.tar.gz
PY=/aios/runtime/python/bin/python3
$PY -m pip install --quiet --no-index --no-deps --find-links "$F"/wheels jinja2 markupsafe 2>/dev/null
rm -rf /aios/runtime/python/lib/python3.13/test /aios/runtime/python/lib/python3.13/idlelib /aios/runtime/python/lib/python3.13/tkinter /aios/runtime/python/share
find /aios/runtime -name __pycache__ -type d -prune -exec rm -rf {} +
install -D -m 0755 "$F/bun" /aios/runtime/bun/bun
ln -sf /aios/runtime/python/bin/python3 /aios/bin/python3; ln -sf /aios/runtime/bun/bun /aios/bin/bun
chown -R root:root /aios && chmod -R a+rX /aios
cd /tmp && $PY -c "import sys, jinja2; print('python', sys.version.split()[0], 'jinja2', jinja2.__version__)" && /aios/runtime/bun/bun --version | sed 's/^/bun /' && echo 'console.log("ts ok", 6*7)' > /tmp/t.ts && /aios/runtime/bun/bun run /tmp/t.ts
du -sh /aios/runtime/python /aios/runtime/bun | tr "\n" " "; echo
