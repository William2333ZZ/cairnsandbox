#!/bin/bash
# 起 Garage 并建好桶与访问密钥(幂等)。密钥只写进 $SECRETS/s3.env(0600,不进 git,不打印)。
# 用法:SECRETS=./secrets DATA_DIR=. deploy/garage/bootstrap.sh
set -e
cd "$(dirname "$0")/../.."
SECRETS=${SECRETS:?set SECRETS}; DATA_DIR=${DATA_DIR:-.}; IMAGE=${GARAGE_IMAGE:-dxflrs/garage:v1.0.1}; BUCKET=${S3_BUCKET:-fc-store}
ENVF=$SECRETS/s3.env; mkdir -p "$DATA_DIR/s3/meta" "$DATA_DIR/s3/data"
umask 077
if [ ! -f "$ENVF" ]; then
  { echo "GARAGE_RPC_SECRET=$(openssl rand -hex 32)"; echo "GARAGE_ADMIN_TOKEN=$(openssl rand -hex 24)"; } > "$ENVF"
fi
if ! docker ps --format '{{.Names}}' | grep -qx fc-s3; then
  docker rm -f fc-s3 >/dev/null 2>&1 || true
  docker run -d --name fc-s3 --restart unless-stopped --network fcnet \
    --env-file <(grep '^GARAGE_' "$ENVF") \
    -v "$PWD/deploy/garage/garage.toml:/etc/garage.toml:ro" \
    -v "$DATA_DIR/s3/meta:/var/lib/garage/meta" -v "$DATA_DIR/s3/data:/var/lib/garage/data" \
    -p 127.0.0.1:13900:3900 "$IMAGE" >/dev/null
  sleep 3
fi
G="docker exec fc-s3 /garage"
NODE=$($G node id -q 2>/dev/null | cut -d@ -f1)
if $G layout show 2>/dev/null | grep -q "No nodes"; then
  $G layout assign -z dc1 -c ${S3_CAPACITY:-1T} "$NODE" >/dev/null
  $G layout apply --version 1 >/dev/null
fi
$G bucket info "$BUCKET" >/dev/null 2>&1 || $G bucket create "$BUCKET" >/dev/null
if ! grep -q '^S3_ACCESS_KEY=' "$ENVF"; then
  OUT=$($G key create fc-store-key)
  { echo "S3_ACCESS_KEY=$(echo "$OUT" | awk '/Key ID/{print $NF}')"; echo "S3_SECRET_KEY=$(echo "$OUT" | awk '/Secret key/{print $NF}')"
    echo "S3_ENDPOINT=http://fc-s3:3900"; echo "S3_BUCKET=$BUCKET"; echo "S3_REGION=garage"; } >> "$ENVF"
fi
$G bucket allow --read --write --owner "$BUCKET" --key fc-store-key >/dev/null
echo "garage ready: bucket=$BUCKET (keys in $ENVF)"
