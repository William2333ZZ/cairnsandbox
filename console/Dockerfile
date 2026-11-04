# syntax=docker/dockerfile:1
# 控制台(Next.js 静态导出)的构建:Node 只在构建镜像里,不进部署。
#   docker build --target check .                      类型检查与单测(CI 用)
#   docker build --target dist --output ../console-dist .   静态文件导出到 ../console-dist(console/build.sh 做这件事),控制面在 /console/ 托管
FROM node:24-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

FROM deps AS check
COPY . .
RUN npm run check

FROM deps AS build
COPY . .
RUN npm run build

FROM scratch AS dist
COPY --from=build /app/out/ /
