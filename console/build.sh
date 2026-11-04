#!/bin/bash
# 把控制台构建成静态文件放进 ../console-dist(控制面只读挂载它,在 /console/ 托管)。需要带 BuildKit 的 docker;Node 工具链留在构建镜像里。
set -e
cd "$(dirname "$0")"
rm -rf ../console-dist.new
DOCKER_BUILDKIT=1 docker build -q --target dist --output "type=local,dest=../console-dist.new" .
rm -rf ../console-dist && mv ../console-dist.new ../console-dist
echo "console-dist: $(find ../console-dist -type f | wc -l) files, $(du -sh ../console-dist | cut -f1)"
