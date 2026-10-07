#!/usr/bin/env bash
# Pins listener/build.sh, the build bin/provision-lane.sh runs on the lane's machine:
#   - `build.sh` runs `go build` static and trimmed inside the one pinned Go image and prints the
#     binary's path and sha256;
#   - `build.sh test` runs gofmt -l, go vet and go test inside the same image;
#   - the image digest is pinned only in build.sh, and CI's listener job runs `build.sh test`, so
#     CI checks with the toolchain the machines build with.
# docker is a stand-in that records its arguments; a copy of build.sh runs in a temp dir, so
# nothing is written to the checkout.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/listener/build.sh"
PIN='golang:1.26.3-bookworm@sha256:386d475a660466863d9f8c766fec64d7fdad3edac2c6a05020c09534d71edb4b'
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }

mkdir -p "$WORK/bin" "$WORK/src"
cp "$BUILD" "$WORK/src/build.sh"
cat > "$WORK/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_CALLS"
src="" prev=""
for arg in "$@"; do
  [ "$prev" = "-v" ] && src="${arg%:/src}"
  prev="$arg"
done
for arg in "$@"; do
  if [ "$arg" = /src/out/lane-listener ]; then
    mkdir -p "$src/out" && printf 'ELF' > "$src/out/lane-listener" && chmod +x "$src/out/lane-listener"
  fi
done
EOF
chmod +x "$WORK/bin/docker"
export DOCKER_CALLS="$WORK/calls"

out="$(PATH="$WORK/bin:$PATH" bash "$WORK/src/build.sh" 2>&1)"
expect $? "build.sh builds"
call="$(head -1 "$WORK/calls")"
case "$call" in *"$PIN go build -trimpath -ldflags=-s -w -o /src/out/lane-listener ."*) ok=0 ;; *) ok=1 ;; esac
expect $ok "the build is go build -trimpath -ldflags='-s -w' in the pinned image ($call)"
case "$call" in *"-e GOTOOLCHAIN=local -e CGO_ENABLED=0 -v $WORK/src:/src -w /src"*) ok=0 ;; *) ok=1 ;; esac
expect $ok "static, with the image's own toolchain, over the listener's directory"
if command -v sha256sum >/dev/null 2>&1; then want="$(printf 'ELF' | sha256sum)"; else want="$(printf 'ELF' | shasum -a 256)"; fi
ok=1
[ "$out" = "binary=$WORK/src/out/lane-listener
sha256=${want%% *}" ] && ok=0
expect $ok "it prints binary=<path> and sha256=<hex> ($out)"

: > "$WORK/calls"
PATH="$WORK/bin:$PATH" bash "$WORK/src/build.sh" test >/dev/null 2>&1
expect $? "build.sh test runs"
call="$(cat "$WORK/calls")"
case "$call" in *"$PIN sh -c "*"gofmt -l ."*"go vet ./..."*"go test ./..."*) ok=0 ;; *) ok=1 ;; esac
expect $ok "test mode runs gofmt -l, go vet and go test in the same pinned image"

PATH="$WORK/bin:$PATH" bash "$WORK/src/build.sh" deploy >/dev/null 2>&1
rc=$?
ok=1
[ "$rc" -eq 2 ] && ok=0
expect $ok "an unknown mode is refused with exit 2 (got $rc)"

# The catalog's workflows are one directory above this kit.
pins="$(grep -rlF "$PIN" "$ROOT/../.github" "$ROOT/bin" "$ROOT/lib" "$ROOT/listener" "$ROOT/tests" --exclude=listener-build-test.sh 2>/dev/null)"
ok=1
[ "$pins" = "$BUILD" ] && ok=0
expect $ok "the image digest is pinned in build.sh only (found in: ${pins//$'\n'/, })"
grep -qE '^        run: lanes/listener/build.sh test$' "$ROOT/../.github/workflows/ci.yml"
expect $? "the catalog's ci.yml runs build.sh test, in its lanes-listener job"

echo
echo "listener-build-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
