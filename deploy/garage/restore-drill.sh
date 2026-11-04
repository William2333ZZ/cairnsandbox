#!/bin/bash
# Garage 的备份与还原演练(D66):用一致的元数据快照 + 数据目录的拷贝,在**另一个容器**里起一份,并从它读回真实的块,逐个核对哈希。
# 不碰在线的 fc-s3;演练完删掉演练目录。用法:SECRETS=./secrets DATA_DIR=<repo> deploy/garage/restore-drill.sh
set -e
trap 'docker rm -f fc-s3-drill >/dev/null 2>&1; docker run --rm -v "$DATA_DIR":/d python:3.12-slim rm -rf /d/s3-drill' EXIT
SECRETS=${SECRETS:?set SECRETS}; DATA_DIR=${DATA_DIR:?set DATA_DIR}; IMAGE=${GARAGE_IMAGE:-dxflrs/garage:v1.0.1}
cd "$(dirname "$0")/../.."
D=$DATA_DIR/s3-drill; NAME=fc-s3-drill; ENVF=$SECRETS/s3.env
as_root() { docker run --rm -v "$DATA_DIR":/d python:3.12-slim sh -c "$1"; }       # Garage's files belong to root (the container's user); copying and deleting them needs the same
as_root "rm -rf /d/s3-drill; mkdir -p /d/s3-drill/meta /d/s3-drill/data"
echo "1. consistent metadata snapshot"
docker exec fc-s3 /garage meta snapshot >/dev/null 2>&1
SNAP=$(as_root 'ls -1d /d/s3/meta/snapshots/* | tail -1'); echo "   $SNAP"
echo "2. copy snapshot + node identity + data (this is the backup)"
# meta/ holds more than the database: cluster_layout and data_layout (without them the restored node says "no role assigned" and answers 503), node_key (its identity), peer_list ...
# so copy every small file except the live database (db.sqlite, -wal, -shm), and take the database from the consistent snapshot.
as_root "cd /d/s3/meta && for f in *; do [ -f \"\$f\" ] && case \"\$f\" in db.sqlite*) ;; *) cp -a \"\$f\" /d/s3-drill/meta/ ;; esac; done; cp -a $SNAP /d/s3-drill/meta/db.sqlite && cp -a /d/s3/data/. /d/s3-drill/data/"
echo "3. start a second Garage from the copy"
docker rm -f $NAME >/dev/null 2>&1 || true
docker run -d --name $NAME --network fcnet --env-file <(grep '^GARAGE_' "$ENVF") -v "$PWD/deploy/garage/garage.toml:/etc/garage.toml:ro" -v "$D/meta:/var/lib/garage/meta" -v "$D/data:/var/lib/garage/data" "$IMAGE" >/dev/null
for i in $(seq 1 30); do docker exec $NAME /garage status >/dev/null 2>&1 && break; sleep 1; done
echo "4. read every chunk back through the S3 API and check its hash"
F=$(umask 077; mktemp); grep '^S3_' "$ENVF" | sed 's#^S3_ENDPOINT=.*#S3_ENDPOINT=http://'$NAME':3900#' > "$F"
docker run --rm --network fcnet --env-file "$F" -v "$PWD/node/imgstore.py:/imgstore.py:ro" python:3.12-slim python -c '
import sys, hashlib; sys.path.insert(0, "/"); import imgstore
import time
be = imgstore.s3_from_env()
for i in range(60):                                  # a restored node needs a few seconds to see itself in the cluster layout (503 until then)
    try: names = [n for n, _, _ in be.list("chunks")]; break
    except Exception as e:
        if i == 59: raise
        time.sleep(1)
bad = 0
for n in names:
    d = be.get("chunks/" + n)
    if d is None or hashlib.sha256(d).hexdigest() != n: bad += 1
print(f"   chunks: {len(names)}  bad: {bad}  images: {len(be.list(chr(105)+chr(109)+chr(97)+chr(103)+chr(101)+chr(115)))}  bundles: {len(be.list(chr(98)+chr(117)+chr(110)+chr(100)+chr(108)+chr(101)+chr(115)))}")
sys.exit(1 if bad or not names else 0)'
RC=$?; rm -f "$F"
echo "5. clean up"; docker rm -f $NAME >/dev/null 2>&1; as_root "rm -rf /d/s3-drill"
[ $RC = 0 ] && echo "RESTORE DRILL OK" || { echo "RESTORE DRILL FAILED"; exit 1; }
