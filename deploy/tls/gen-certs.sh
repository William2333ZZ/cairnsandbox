#!/bin/bash
# 给控制面 <-> 节点这一跳做 TLS(D62):一个自建 CA + 每个节点一张证书。私钥只在你给的目录里,不进 git。
# 用法:deploy/tls/gen-certs.sh <输出目录> <节点主机名> [节点主机名 ...]      例:gen-certs.sh ./secrets/tls fc-node fc-node-2
# 产物:ca.crt(控制面的 NODE_CA_FILE)、<名>.crt / <名>.key(节点的 TLS_CERT / TLS_KEY)。证书有效期 1 年,主机名进 SAN。
set -e
OUT=${1:?output dir}; shift; [ $# -ge 1 ] || { echo "give at least one node hostname"; exit 1; }
umask 077; mkdir -p "$OUT"; cd "$OUT"
if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 1825 -subj "/CN=cairn-node-ca" -keyout ca.key -out ca.crt 2>/dev/null
fi
for h in "$@"; do
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$h" -keyout "$h.key" -out "$h.csr" 2>/dev/null
  printf "subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" "$h" > "$h.ext"
  openssl x509 -req -in "$h.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 -extfile "$h.ext" -out "$h.crt" 2>/dev/null
  rm -f "$h.csr" "$h.ext"; echo "node cert: $OUT/$h.crt"
done
echo "CA: $OUT/ca.crt (give it to the control plane as NODE_CA_FILE); keep ca.key off the nodes"
