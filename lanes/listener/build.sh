#!/usr/bin/env bash
# build.sh: build or test the scale-set listener inside the pinned Go image.
#
# Targets: the lane's machine (bin/provision-lane.sh runs `build.sh` as root and installs the
# binary it prints) and CI (`build.sh test`). Both are Linux with Docker; nothing else is needed on
# the host: the pinned golang image brings the toolchain (GOTOOLCHAIN=local, so it never downloads
# another) and fetches the modules go.sum pins. The binary is static (CGO_ENABLED=0), for the
# Docker host's own platform, which is the machine when the provisioner runs it. The image pin
# lives only here, so CI tests with the toolchain the machines build with.
#
#   build.sh         build out/lane-listener, then print binary=<path> and sha256=<hex>
#   build.sh test    gofmt -l (must list nothing), go vet ./..., go test ./...
set -euo pipefail

IMAGE='golang:1.26.3-bookworm@sha256:386d475a660466863d9f8c766fec64d7fdad3edac2c6a05020c09534d71edb4b'
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Runs as the caller's uid, so out/ belongs to whoever built it; HOME=/tmp gives that uid a
# writable Go build cache inside the throwaway container.
go_in_image() {
  docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOTOOLCHAIN=local -e CGO_ENABLED=0 \
    -v "$SRC":/src -w /src "$IMAGE" "$@"
}

case "${1:-build}" in
  build)
    go_in_image go build -trimpath -ldflags='-s -w' -o /src/out/lane-listener .
    bin="$SRC/out/lane-listener"
    [ -x "$bin" ] || { echo "build.sh: the build left no binary at $bin" >&2; exit 1; }
    if command -v sha256sum >/dev/null 2>&1; then sum="$(sha256sum "$bin")"; else sum="$(shasum -a 256 "$bin")"; fi
    echo "binary=$bin"
    echo "sha256=${sum%% *}"
    ;;
  test)
    # shellcheck disable=SC2016 # expanded by the image's shell, not this one
    go_in_image sh -c 'unformatted="$(gofmt -l .)"; if [ -n "$unformatted" ]; then echo "gofmt -l lists:"; echo "$unformatted"; exit 1; fi; go vet ./... && go test ./...'
    ;;
  *)
    echo "usage: $0 [build|test]" >&2
    exit 2
    ;;
esac
