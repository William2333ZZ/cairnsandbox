#!/usr/bin/env bash
# $1 = workspace. Last stdout line is `SCORE x`. Reads only $1 and this directory; never depends on cwd.
set -euo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="$(cd "$1" && pwd)"
exec python3 -I "$D/check.py" "$WS"
