#!/usr/bin/env bash
# build-runner-image.sh: build a lane's runner image from image/, preload the lane's inner Docker
# store with the Supabase images each preloaded repository's own `supabase start` pulls, verify it,
# and move <image>:current to it. <image> is the lane's `image`, and its state lives under
# /var/lib/<lane name> (the lane provisioner's state directory).
#
# The store is not in the image. It is written to <store>/golden-<tag> on the lane's XFS store
# filesystem (the provisioner's <state>/store, reflink-capable), which the preload container has
# mounted on /var/lib/docker; each job then mounts a reflink snapshot of it on its own
# /var/lib/docker (bin/lane-slot.sh), so Sysbox copies nothing at container start and the image
# stays the tools alone. An image tag without its store is not a usable image: the converge paths
# below ignore one and build again.
#
# Linux-only: runs as root on the lane's machine (Ubuntu 24.04 amd64, bash 5, GNU coreutils,
# util-linux), from <lane name>-image-build.timer (written by bin/provision-lane.sh) and by the
# operator for a lane's first image. It drives the host Docker daemon and needs the sysbox-runc
# runtime registered with it. tests/build-runner-image-test.sh runs it with docker, gh, runuser and
# timeout stubbed.
#
# usage: build-runner-image.sh <host> <lane> [--refresh] [--if-provisioned]
#   (default)         Converge on the image definition: when an <image>:<date>-<hash> of the
#                     current definition exists, point current at the newest one (a reverted pin
#                     returns to its verified image without a rebuild); otherwise build
#                     <today>-<hash>.
#   --refresh         Converge on today: build <today>-<hash> unless it exists. The weekly timer uses
#                     it, which is what picks up upstream config.toml and Supabase image changes.
#   --if-provisioned  Exit 0 with a note when the lane does not run here yet: the host Docker has no
#                     sysbox-runc runtime, or the lane's store filesystem is absent. Without it each
#                     is a failure.
#
# Which repositories: the lane's `preload` map in hosts/<host>.yml (lib/lanes.py validates it) says
# where each preloaded repository keeps its Supabase project (`<repo>: <dir>`, or `<repo>: "-"`
# for none). Each config is read at repos/<owner>/<repo>/contents/<dir>/config.toml. A map with no
# project (empty, or every entry "-") preloads nothing: no config is read, the build container
# records an empty manifest instead of running the preload, the store stays empty, and step 5
# skips verify-preload; the image is still built, checked, tagged and made current.
#
# Tag: <UTC date YYYYMMDD>-<first 12 hex of a sha256 over every file in image/ except *.md, and the
# lane's preload map in canonical text>. It covers the Dockerfile and everything it copies, and
# this lane's preload map, so any change to the image definition is a new tag and another lane's
# map is not.
#
# Build, following Sysbox's documented "docker commit" preloading procedure, so the host's default
# runtime stays runc:
#   1. read each preloaded repository's <dir>/config.toml with `gh api` as the lane's user (with its
#      home, so its gh reads the lane's App token), into a staging tree laid out
#      <name>/supabase/config.toml, plus empty stand-ins for the email-template files it names:
#      the CLI refuses a config whose template file is missing, and template content has no bearing
#      on which images start pulls. Nothing else of the repository is fetched;
#   2. docker build the base image as <image>:base-<tag> (linux/amd64, runc);
#   3. run it under sysbox-runc in `hold` mode with the empty <store>/golden-<tag>.new mounted on
#      /var/lib/docker, copy the staging tree to /opt/known-ci/preload (it stays in the image: the
#      lane's zero-pull proof starts these same projects), and exec its `preload` mode:
#      `supabase start` and `supabase stop --no-backup` per project, then dockerd stopped; every
#      pulled image lands in the mounted store, not the container's rootfs;
#   4. docker commit the container to <image>:<tag>, resetting CMD (the container ran with
#      CMD hold); the mounted store is not part of the commit;
#   5. verify CMD, ENTRYPOINT and the ai-fleet.disk-watch=keep label; rename the store to <store>/golden-<tag>; and check that a fresh
#      sysbox-runc container finds, in `verify-preload`, exactly the images the preload recorded,
#      on a reflink snapshot of that store made and removed by bin/lane-slot.sh, the way a job gets
#      its store;
#   6. tag the image <image>:current and write KNOWN_CI_IMAGE_TAG=<tag> to
#      /var/lib/<lane name>/image.env, which the lane's units read at every start; then prune dated
#      tags beyond the newest three and every dated tag without its store (never the one current
#      names), every base-* tag, every store whose tag has no image, and the Docker build cache
#      records unused for a week, so the lane's builds leave nothing for another job to clean up.
# Any failure exits non-zero with the reason, removes the build container, this run's partial tags
# and its store, and leaves current where it was.
#
# Concurrency: callers hold /run/<lane name>-runner-build.lock (the timer's unit runs it under flock).
# shellcheck disable=SC2153 # NAME, OWNER, IMAGE, LANE_USER and LANE_HOME come from lib/lanes.py env
set -euo pipefail

log() { printf '[runners-build] %s\n' "$*"; }
# stdout feeds the timer's journal and stderr reaches the operator, so a failure goes to both.
die() {
  printf '[runners-build FAIL] %s\n' "$*"
  printf '[runners-build FAIL] %s\n' "$*" >&2
  exit 2
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT="$REPO_ROOT/image"
SLOT_HELPER="$REPO_ROOT/bin/lane-slot.sh"
KEEP=3
# Build-cache records unused for this long are dropped after every run (a week: the timer's period).
CACHE_MAX_AGE=168h
ENTRYPOINT_JSON='["/usr/local/bin/known-ci-entrypoint"]'
PRELOAD_DIR=/opt/known-ci/preload
MANIFEST=/etc/known-ci-runner/preloaded-images
KEEP_LABEL_KEY=ai-fleet.disk-watch
KEEP_LABEL_VALUE=keep
TODAY="${RUNNERS_BUILD_TODAY:-$(date -u +%Y%m%d)}"
STEP_TIMEOUT="${RUNNERS_BUILD_STEP_TIMEOUT:-3600}"
REFRESH=0
IF_PROVISIONED=0
HOST=""
LANE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --refresh) REFRESH=1 ;;
    --if-provisioned) IF_PROVISIONED=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | grep -v '^# shellcheck'; exit 0 ;;
    -*) die "unknown argument: $1" ;;
    *)
      if [ -z "$HOST" ]; then HOST="$1"
      elif [ -z "$LANE" ]; then LANE="$1"
      else die "unexpected argument: $1"; fi ;;
  esac
  shift
done
[ -n "$HOST" ] && [ -n "$LANE" ] || die "usage: $0 <host> <lane> [--refresh] [--if-provisioned]"
[[ "$HOST" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid host '$HOST' (allowed: a-z 0-9 -)"

if [ "${RUNNERS_ALLOW_NON_ROOT:-0}" != 1 ] && [ "$(id -u)" != 0 ]; then
  die "must run as root (it drives the host Docker daemon)"
fi
[[ "$TODAY" =~ ^[0-9]{8}$ ]] || die "invalid build date '$TODAY'"

STAGE=""
BUILD_CTR=""
BASE_TAG=""
NEW_TAG=""
NEW_STORE=""
cleanup() {
  if [ -n "$BUILD_CTR" ]; then docker rm -f "$BUILD_CTR" >/dev/null 2>&1 || true; fi
  if [ -n "$NEW_TAG" ]; then docker image rm "$NEW_TAG" >/dev/null 2>&1 || true; fi
  if [ -n "$BASE_TAG" ]; then docker image rm "$BASE_TAG" >/dev/null 2>&1 || true; fi
  if [ -n "$NEW_STORE" ]; then rm -rf "$NEW_STORE" "$STORE_DIR/verify-$$"; fi
  if [ -n "$STAGE" ]; then rm -rf "$STAGE"; fi
}
trap cleanup EXIT

# The preloaded store for an image tag, and whether the tag is usable (image and store both there).
store_of() { printf '%s/golden-%s' "$STORE_DIR" "$1"; }
has_store() { [ -d "$(store_of "$1")" ]; }

# The image ID a tag names, or nothing when the tag does not exist.
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

# Every tag of $IMAGE, one per line. Callers assign it on its own line so a failure stops the run.
list_tags() { docker image ls "$IMAGE" --format '{{.Tag}}'; }
only_dated() { grep -E '^[0-9]{8}-[0-9a-f]{12}$' || true; }

definition_hash() {
  python3 - "$CONTEXT" "$PRELOAD_TEXT" <<'PY'
import hashlib
import os
import sys

root, preload = sys.argv[1], sys.argv[2]
digest = hashlib.sha256()
for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()
    for name in sorted(filenames):
        if name.endswith(".md"):
            continue
        path = os.path.join(dirpath, name)
        digest.update(os.path.relpath(path, root).encode() + b"\0")
        with open(path, "rb") as fh:
            digest.update(fh.read())
        digest.update(b"\0")
digest.update(b"preload\0" + preload.encode() + b"\0")
print(digest.hexdigest()[:12])
PY
}

# Run a step with its (long) output kept aside; on failure show the tail and stop.
run_logged() {
  local label="$1"
  shift
  if ! "$@" >"$STAGE/$label.log" 2>&1; then
    printf '[runners-build FAIL] %s failed; last output:\n' "$label" >&2
    tail -n 40 "$STAGE/$label.log" >&2 || true
    die "$label failed: $*"
  fi
}

prune() {
  local current_id tags tag created n=0 sorted="" dir
  current_id="$(image_id "$IMAGE:current")"
  tags="$(list_tags)" || die "docker image ls failed"
  for tag in $(printf '%s\n' "$tags" | only_dated); do
    created="$(docker image inspect -f '{{.Created}}' "$IMAGE:$tag")" || continue
    sorted+="$created $tag"$'\n'
  done
  while read -r created tag; do
    [ -n "$tag" ] || continue
    # A tag without its store cannot run a job (prepare fails closed): it goes whatever its age and
    # does not count toward the kept three.
    if has_store "$tag"; then
      n=$((n + 1))
      [ "$n" -gt "$KEEP" ] || continue
    fi
    [ "$(image_id "$IMAGE:$tag")" != "$current_id" ] || continue
    if docker image rm "$IMAGE:$tag" >/dev/null 2>&1; then
      log "pruned $IMAGE:$tag"
    else
      log "WARN could not remove $IMAGE:$tag (a container still uses it?)"
    fi
  done < <(printf '%s' "$sorted" | LC_ALL=C sort -r)
  for tag in $(printf '%s\n' "$tags" | grep '^base-' || true); do
    if docker image rm "$IMAGE:$tag" >/dev/null 2>&1; then log "removed stale $IMAGE:$tag"; fi
  done
  # A store whose image is gone, and any unfinished store an interrupted run left behind.
  for dir in "$STORE_DIR"/golden-*; do
    [ -d "$dir" ] || continue
    tag="${dir##*/golden-}"
    case "$tag" in
      *.new) ;;
      *) [ -z "$(image_id "$IMAGE:$tag")" ] || continue ;;
    esac
    rm -rf "$dir" && log "pruned store $dir"
  done
  # The base build leaves its layers in Docker's build cache, and the layers of a tag removed above
  # stay there with nothing sharing them. Nothing else on the machine is trusted to clear them, so
  # the build drops every cache record unused for a week (the timer's own period): the next build
  # re-creates what it needs. It is the machine's whole build cache, not only this lane's, and a
  # cache is all it is. A failure is a warning: the image is already built and current.
  if docker builder prune --force --filter "until=$CACHE_MAX_AGE" >/dev/null 2>&1; then
    log "pruned build cache unused for $CACHE_MAX_AGE"
  else
    log "WARN could not prune the Docker build cache (docker builder prune)"
  fi
}

# Exit 0 with a note under --if-provisioned (the timer), fail otherwise (a direct caller).
not_here() {
  if [ "$IF_PROVISIONED" = 1 ]; then
    log "$1; nothing to build"
    exit 0
  fi
  die "$1"
}

as_lane_user() {
  runuser -u "$LANE_USER" -- env -u GH_TOKEN -u GITHUB_TOKEN HOME="$LANE_HOME" GH_CONFIG_DIR="$LANE_HOME/.config/gh" GH_PROMPT_DISABLED=1 "$@"
}

# Empty stand-ins for the email templates a config.toml names, where Supabase CLI v2 resolves them:
# auth.email.template.* from the repository root (the supabase directory's parent),
# auth.email.notification.* from the supabase directory.
template_standins() {
  python3 - "$1" <<'PY'
import os
import sys
import tomllib

supabase_dir = os.path.abspath(sys.argv[1])
project_root = os.path.dirname(supabase_dir)
with open(os.path.join(supabase_dir, "config.toml"), "rb") as fh:
    email = (tomllib.load(fh).get("auth") or {}).get("email") or {}
for section, base in (("template", project_root), ("notification", supabase_dir)):
    for name, entry in (email.get(section) or {}).items():
        path = (entry or {}).get("content_path")
        if not path:
            continue
        if os.path.isabs(path):
            sys.exit(f"auth.email.{section}.{name}.content_path is absolute: {path}")
        target = os.path.normpath(os.path.join(base, path))
        if os.path.commonpath([target, project_root]) != project_root:
            sys.exit(f"auth.email.{section}.{name}.content_path leaves the repository: {path}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "a").close()
        print(f"stand-in {os.path.relpath(target, project_root)}")
PY
}

PROJECT_ROOTS=()
PROJECT_REPOS=()
PROJECT_DIRS=()
# The lane's preload map, minus the repositories marked "-".
map_projects() {
  local name dir
  while read -r name dir; do
    [ -n "$name" ] && [ "$dir" != - ] || continue
    PROJECT_REPOS+=("$OWNER/$name")
    PROJECT_DIRS+=("$dir")
  done <<< "$PRELOAD_TEXT"
  [ "${#PROJECT_REPOS[@]}" -gt 0 ] || log "no repository to preload: the $NAME lane's preload map lists no Supabase project"
}

stage_projects() {
  local i repo dir name dest out
  for i in "${!PROJECT_REPOS[@]}"; do
    repo="${PROJECT_REPOS[$i]}"
    dir="${PROJECT_DIRS[$i]}"
    name="${repo##*/}"
    dest="$STAGE/projects/$name/supabase"
    mkdir -p "$dest"
    as_lane_user gh api -H 'Accept: application/vnd.github.raw' "repos/$repo/contents/$dir/config.toml" \
      </dev/null >"$dest/config.toml" \
      || die "cannot read $repo $dir/config.toml with gh as $LANE_USER (does its token reach $repo?)"
    [ -s "$dest/config.toml" ] || die "$repo $dir/config.toml came back empty"
    out="$(template_standins "$dest")" || die "$repo $dir/config.toml: cannot place template stand-ins"
    [ -z "$out" ] || log "$repo: $out"
    PROJECT_ROOTS+=("$PRELOAD_DIR/$name")
    log "staged $repo $dir/config.toml as $PRELOAD_DIR/$name/supabase/config.toml"
  done
}

# Point current and the lane's image.env at <tag>; rewrites image.env only when it differs.
point_current() {
  local tag="$1" want tmp
  if [ "$(image_id "$IMAGE:current")" != "$(image_id "$IMAGE:$tag")" ]; then
    docker tag "$IMAGE:$tag" "$IMAGE:current" || die "cannot tag $IMAGE:$tag as $IMAGE:current"
  fi
  want="KNOWN_CI_IMAGE_TAG=$tag"
  [ "$(cat "$IMAGE_ENV" 2>/dev/null || true)" != "$want" ] || return 0
  mkdir -p "$STATE_DIR"
  tmp="$(mktemp "$IMAGE_ENV.XXXXXX")" || die "cannot write in $STATE_DIR"
  if ! { printf '%s\n' "$want" >"$tmp" && chmod 0644 "$tmp" && mv -f "$tmp" "$IMAGE_ENV"; }; then
    rm -f "$tmp"
    die "cannot write $IMAGE_ENV"
  fi
  log "$IMAGE_ENV -> $want"
}

# docker cp writes the files with the host's uid 0, which a Sysbox container's user namespace shows
# as nobody, so the container's root could not write inside them: the first preload died with
# "PermissionDenied: FileSystem.makeDirectory (.../supabase/.branches)" (2026-09-26). Extracting
# inside the container creates every file as the container's own root.
copy_projects() {
  tar -C "$STAGE/projects" -cf - . | docker exec -i "$BUILD_CTR" tar -C "$PRELOAD_DIR" -xf -
}

build_image() {
  local tag="$1" cmd entry keep preloaded store
  BASE_TAG="$IMAGE:base-$tag"
  BUILD_CTR="$IMAGE-build-$tag"
  docker rm -f "$BUILD_CTR" >/dev/null 2>&1 || true
  store="$(store_of "$tag")"
  NEW_STORE="$store.new"
  rm -rf "$NEW_STORE"
  install -d -m 0700 "$NEW_STORE" || die "cannot create $NEW_STORE (is the store filesystem mounted?)"

  log "building $BASE_TAG (linux/amd64)"
  run_logged build docker build --platform linux/amd64 --progress=plain -t "$BASE_TAG" "$CONTEXT"

  # The empty store mounted on /var/lib/docker takes the preload's pulls; with a mount there Sysbox
  # neither copies the image's /var/lib/docker in nor syncs anything back at stop.
  log "preloading in $BUILD_CTR under sysbox-runc into $NEW_STORE: ${PROJECT_ROOTS[*]:-no project}"
  run_logged run docker run -d --runtime=sysbox-runc -v "$NEW_STORE:/var/lib/docker" --name "$BUILD_CTR" "$BASE_TAG" hold
  if [ "${#PROJECT_ROOTS[@]}" -gt 0 ]; then
    run_logged copy copy_projects
    run_logged preload timeout "$STEP_TIMEOUT" docker exec "$BUILD_CTR" known-ci-entrypoint preload "${PROJECT_ROOTS[@]}"
    # The entrypoint's own summary lines (image count, inner store size) belong in this log.
    grep '^\[runner\] ' "$STAGE/preload.log" | sed 's/^\[runner\] /[runners-build] preload: /' || true
  else
    # Nothing to preload: an empty manifest records that, so the lane's smoke does not read the
    # missing projects as a broken image. The store stays empty and each job's dockerd starts on it.
    run_logged manifest docker exec "$BUILD_CTR" sh -c "mkdir -p ${MANIFEST%/*} && : > $MANIFEST"
  fi
  preloaded="$(docker exec "$BUILD_CTR" cat "$MANIFEST")" || die "cannot read $MANIFEST from $BUILD_CTR"

  NEW_TAG="$IMAGE:$tag"
  run_logged commit docker commit --change 'CMD []' "$BUILD_CTR" "$NEW_TAG"
  docker rm -f "$BUILD_CTR" >/dev/null
  BUILD_CTR=""

  cmd="$(docker image inspect -f '{{json .Config.Cmd}}' "$NEW_TAG")" || die "cannot inspect $NEW_TAG"
  case "$cmd" in null|'[]') ;; *) die "$NEW_TAG kept CMD $cmd; a job container would not run the runner" ;; esac
  entry="$(docker image inspect -f '{{json .Config.Entrypoint}}' "$NEW_TAG")" || die "cannot inspect $NEW_TAG"
  [ "$entry" = "$ENTRYPOINT_JSON" ] || die "$NEW_TAG has ENTRYPOINT $entry, expected $ENTRYPOINT_JSON"
  keep="$(docker image inspect -f "{{index .Config.Labels \"$KEEP_LABEL_KEY\"}}" "$NEW_TAG")" || die "cannot inspect $NEW_TAG"
  [ "$keep" = "$KEEP_LABEL_VALUE" ] || die "$NEW_TAG lacks the label $KEEP_LABEL_KEY=$KEEP_LABEL_VALUE; a machine's legacy disk-watch job would prune it between jobs"
  rm -rf "$store"
  mv "$NEW_STORE" "$store" || die "cannot move $NEW_STORE to $store"
  NEW_STORE="$store"
  if [ "${#PROJECT_ROOTS[@]}" -gt 0 ]; then
    log "verifying $NEW_TAG on a snapshot of $store in a fresh sysbox-runc container"
    run_logged snapshot env KNOWN_CI_STORE_DIR="$STORE_DIR" KNOWN_CI_IMAGE_TAG="$tag" "$SLOT_HELPER" snapshot "verify-$$"
    run_logged verify timeout "$STEP_TIMEOUT" docker run --rm --runtime=sysbox-runc -v "$STORE_DIR/verify-$$:/var/lib/docker" "$NEW_TAG" verify-preload
    env KNOWN_CI_STORE_DIR="$STORE_DIR" KNOWN_CI_IMAGE_TAG="$tag" "$SLOT_HELPER" discard "verify-$$" \
      || log "WARN could not remove the verify snapshot $STORE_DIR/verify-$$"
  else
    log "nothing preloaded, so no verify-preload (it holds a store to a manifest that lists images)"
  fi

  NEW_STORE=""
  point_current "$tag"
  NEW_TAG=""
  [ -z "$preloaded" ] || printf '%s\n' "$preloaded" | sed 's/^/[runners-build] preloaded /'
  log "store -> $store ($(du -sh "$store" 2>/dev/null | cut -f1))"
  log "current -> $IMAGE:$tag"
}

# The host file lives in the machine's values checkout, the owner's private repository.
HOST_YML="${RUNNERS_HOST_YML:-${RUNNERS_CONFIG:-/opt/runner-lanes-config}/hosts/$HOST.yml}"
settings="$(python3 "$REPO_ROOT/lib/lanes.py" env "$HOST_YML" "$LANE")" || die "cannot read the $LANE lane from $HOST_YML"
while IFS='=' read -r key value; do
  case "$key" in NAME|OWNER|IMAGE|LANE_USER|LANE_HOME) printf -v "$key" '%s' "$value" ;; esac
done <<< "$settings"
PRELOAD_TEXT="$(python3 "$REPO_ROOT/lib/lanes.py" preload "$HOST_YML" "$LANE")" || die "cannot read the $LANE lane's preload map from $HOST_YML"
STATE_DIR="${RUNNERS_LANE_STATE_DIR:-/var/lib/$NAME}"
STORE_DIR="${RUNNERS_LANE_STORE_DIR:-$STATE_DIR/store}"
IMAGE_ENV="$STATE_DIR/image.env"
map_projects
runtimes="$(docker info --format '{{json .Runtimes}}')" || die "docker info failed; is the host Docker daemon running?"
[[ "$runtimes" == *'"sysbox-runc"'* ]] \
  || not_here "no sysbox-runc runtime on this host: the $NAME lane is not provisioned here (bin/provision-host.sh installs Sysbox)"
# The store filesystem must be there and take reflinks (XFS): a job's snapshot is one.
reflink_probe="$STORE_DIR/.reflink-probe.$$"
if ! { [ -d "$STORE_DIR" ] && : > "$reflink_probe" && cp --reflink=always "$reflink_probe" "$reflink_probe.copy"; } 2>/dev/null; then
  rm -f "$reflink_probe" "$reflink_probe.copy"
  not_here "no reflink-capable store at $STORE_DIR: the $NAME lane's store filesystem is not provisioned here (bin/provision-lane.sh mounts it)"
fi
rm -f "$reflink_probe" "$reflink_probe.copy"

HASH="$(definition_hash)" || die "cannot hash $CONTEXT"
TAG="$TODAY-$HASH"
all_tags="$(list_tags)" || die "docker image ls failed"
if [ "$REFRESH" = 1 ]; then
  candidates="$(printf '%s\n' "$all_tags" | only_dated | grep -Fx "$TAG" || true)"
else
  candidates="$(printf '%s\n' "$all_tags" | only_dated | grep -E -- "-$HASH\$" | LC_ALL=C sort -r || true)"
fi
existing=""
for tag in $candidates; do
  if has_store "$tag"; then
    [ -n "$existing" ] || existing="$tag"
  else
    # Useless to the lane and, if it stayed, this run's commit could not take its tag back safely.
    log "$IMAGE:$tag has no store at $(store_of "$tag"); untagging it and building again"
    docker image rm "$IMAGE:$tag" >/dev/null 2>&1 || true
  fi
done
if [ -n "$existing" ]; then
  existing_id="$(docker image inspect -f '{{.Id}}' "$IMAGE:$existing")" || die "cannot inspect $IMAGE:$existing"
  if [ "$existing_id" = "$(image_id "$IMAGE:current")" ]; then
    log "converged: current is $IMAGE:$existing"
  else
    log "current -> $IMAGE:$existing (already built and verified)"
  fi
  point_current "$existing"
  prune
  exit 0
fi

log "building $IMAGE:$TAG"
STAGE="$(mktemp -d)"
stage_projects
build_image "$TAG"
prune
