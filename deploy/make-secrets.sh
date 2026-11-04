#!/bin/bash
# Generate the two secrets the stack needs when they do not exist yet: the control-plane bearer and the Postgres password (0600, never printed).
set -e
D=${1:-$(dirname "$0")/secrets}; mkdir -p "$D"; chmod 700 "$D"
[ -s "$D/cp.token" ] || { umask 077; head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$D/cp.token"; echo "wrote $D/cp.token"; }
[ -s "$D/db.password" ] || { umask 077; head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$D/db.password"; echo "wrote $D/db.password"; }
echo "secrets in $D: $(ls "$D" | tr '\n' ' ')"
