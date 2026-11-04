# syntax=docker/dockerfile:1
# JS/TS SDK 的类型检查与单测(CI 用):docker build --target check sdk/js
FROM node:24-bookworm-slim AS check
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run check
