# fc-cp: the control plane (cp/). Modules come through GOPROXY (--build-arg GOPROXY=https://goproxy.cn,direct behind a regional mirror); the module cache is a BuildKit cache
# mount, so a rebuild downloads nothing. The tests run in the build: a broken control plane cannot become an image.
#   docker build -t fc-cp:dev cp/
FROM golang:1.26-alpine AS build
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=$GOPROXY CGO_ENABLED=0 GOFLAGS=-mod=mod
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod go vet ./... && go test ./... && go build -trimpath -ldflags="-s -w" -o /fc-cp .

FROM alpine:3.20
COPY --from=build /fc-cp /usr/local/bin/fc-cp
WORKDIR /var/lib/fc-cp
ENTRYPOINT ["/usr/local/bin/fc-cp"]
