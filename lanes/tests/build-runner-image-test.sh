#!/bin/bash
# build-runner-image-test.sh: hermetic tests for bin/build-runner-image.sh.
# Stubs docker (with a small tag store), gh, runuser, timeout and cp (for --reflink=always, which
# neither tmpfs nor ext4 takes) on PATH, and the slot helper the build asks for its verify snapshot
# (tests/lane-slot-test.sh covers the real one), and runs a copy of the script against a copy of
# image/; no daemon, Sysbox, network or GitHub involved.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }

DAY1=20260925
PRELOAD="/opt/known-ci/preload"
# The org lane's repositories with a Supabase project, in the preload map's sorted order; `tools` has none.
ROOTS="$PRELOAD/alpha $PRELOAD/beta $PRELOAD/delta $PRELOAD/gamma $PRELOAD/omega"

write_host() {
  cat > "$TMP/s/host.yml" <<'YML'
hostname: box-1
sysbox: {image: /var/lib/runners/sysbox.img, disk_gb: 20}
lanes:
  - name: box-ci
    scope: org
    owner: acme
    runner_group: box-ci
    repos: [alpha, beta, gamma, delta, omega, tools]
    labels: {ci: box-ci}
    app: {id: 123456, installation_id: 7654321, key_file: /etc/box-ci/app.pem, login: acme-bot}
    slots: 2
    min_runners: 1
    name_prefix: box-lane
    image: box-ci-runner
    preload: {alpha: supabase, beta: packages/database/supabase, gamma: supabase, delta: supabase, omega: supabase, tools: "-"}
    slice: {memory_max: 16G, memory_high: 14G, cpu_quota: "800%", cpu_weight: 50}
    container: {memory: 4g, pids: 2048, tmp_size: 2g}
    runtime_max_sec: 3600
    slot_disk_gb: 10
    store_disk_gb: 40
    check_ports: [22]
  - name: own-ci
    scope: user
    owner: solo
    labels: {ci: own-ci}
    exclude: []
    app: {id: 234567, installation_id: 8765432, key_file: /etc/own-ci/app.pem, login: solo-bot}
    slots: 2
    name_prefix: own-lane
    image: own-ci-runner
    preload: {}
    slice: {memory_max: 8G, memory_high: 7G, cpu_quota: "400%", cpu_weight: 30}
    container: {memory: 4g, pids: 2048, tmp_size: 2g}
    runtime_max_sec: 3600
    slot_disk_gb: 6
    store_disk_gb: 20
    check_ports: [22]
YML
}

# host_variant <out> <python statement on `lanes`, a name -> lane map>: the host file with one change.
host_variant() {
  python3 - "$TMP/s/host.yml" "$1" "$2" <<'PY'
import sys
import yaml

doc = yaml.safe_load(open(sys.argv[1]))
lanes = {lane["name"]: lane for lane in doc["lanes"]}
exec(sys.argv[3])
yaml.safe_dump(doc, open(sys.argv[2], "w"), sort_keys=False)
PY
}

setup() {
  rm -rf "$TMP/s"
  mkdir -p "$TMP/s/bin" "$TMP/s/lib" "$TMP/s/pathbin" "$TMP/s/fixtures" "$TMP/s/state/store" \
    "$TMP/s/docker/tags" "$TMP/s/docker/ctr" "$TMP/s/docker/cmd" "$TMP/s/docker/cp"
  cp "$ROOT/bin/build-runner-image.sh" "$TMP/s/bin/"
  cp "$ROOT"/lib/*.py "$TMP/s/lib/"
  # The slot helper as the build calls it (snapshot <name> / discard <name> with KNOWN_CI_STORE_DIR
  # and KNOWN_CI_IMAGE_TAG), logging the call; it refuses a snapshot of a store that is not there.
  cat > "$TMP/s/bin/lane-slot.sh" <<'SH'
#!/bin/bash
echo "lane-slot $* store=$KNOWN_CI_STORE_DIR tag=$KNOWN_CI_IMAGE_TAG" >> "$CALLLOG"
case "$1" in
  snapshot) [ -d "$KNOWN_CI_STORE_DIR/golden-$KNOWN_CI_IMAGE_TAG" ] || exit 1; mkdir -p "$KNOWN_CI_STORE_DIR/$2" ;;
  discard) rm -rf "$KNOWN_CI_STORE_DIR/$2" ;;
  *) exit 2 ;;
esac
SH
  chmod +x "$TMP/s/bin/lane-slot.sh"
  cp -R "$ROOT/image" "$TMP/s/"
  CTX="$TMP/s/image"
  write_host
  for name in alpha beta gamma delta epsilon; do
    printf 'project_id = "%s"\n[db]\nmajor_version = 17\n' "$name" > "$TMP/s/fixtures/$name.toml"
  done
  # omega names email templates: a template (resolved from the repository root) and a
  # notification (resolved from the supabase directory).
  cat > "$TMP/s/fixtures/omega.toml" <<'TOML'
project_id = "omega"
[db]
major_version = 15
[auth.email.template.invite]
subject = "Invite"
content_path = "./supabase/templates/invite.html"
[auth.email.notification.password_changed]
enabled = true
content_path = "./templates/password_changed.html"
TOML
  CALLLOG="$TMP/s/calls.log"; : > "$CALLLOG"
  export CALLLOG DSTATE="$TMP/s/docker" FIXTURES="$TMP/s/fixtures"
  mkstubs
}

mkstubs() {
  local p="$TMP/s/pathbin"
  # docker: records every call; keeps each image's tags as files <image>:<tag> holding "<id> <created>".
  cat > "$p/docker" <<'SH'
#!/bin/bash
echo "docker $*" >> "$CALLLOG"
S="$DSTATE"
new_entry() {
  local n
  n=$(( $(cat "$S/counter" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$S/counter"
  printf 'sha256:%064d 2026-01-01T%02d:%02d:00Z\n' "$n" $((n / 60)) $((n % 60))
}
case "$1" in
  info)
    [ "${DOCKER_DOWN:-0}" = 1 ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    if [ "${NO_SYSBOX:-0}" = 1 ]; then echo '{"runc":{"path":"runc"}}'; else echo '{"runc":{"path":"runc"},"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}'; fi ;;
  image)
    case "$2" in
      inspect)
        f="$S/tags/$5"; [ -f "$f" ] || exit 1
        read -r id created < "$f"
        case "$4" in
          '{{.Id}}') echo "$id" ;;
          '{{.Created}}') echo "$created" ;;
          '{{json .Config.Cmd}}') cat "$S/cmd/${id#sha256:}" 2>/dev/null || echo null ;;
          '{{json .Config.Entrypoint}}') echo "${STUB_ENTRYPOINT:-[\"/usr/local/bin/known-ci-entrypoint\"]}" ;;
          '{{index .Config.Labels "ai-fleet.disk-watch"}}') echo "${STUB_KEEP_LABEL-keep}" ;;
        esac ;;
      ls) for f in "$S/tags/$3":*; do [ -e "$f" ] && printf '%s\n' "${f##*:}"; done ;;
      rm)
        case " ${IN_USE:-} " in *" ${3#*:} "*) echo "image is being used by running container" >&2; exit 1 ;; esac
        [ -f "$S/tags/$3" ] || exit 1
        rm -f "$S/tags/$3" ;;
    esac ;;
  build)
    [ "${BUILD_FAIL:-0}" = 1 ] && { echo "ERROR: failed to solve: process did not complete"; exit 1; }
    echo "${*: -1}" > "$S/build_context"
    new_entry > "$S/tags/${*: -2:1}" ;;
  run)
    if [ "$2" = -d ]; then
      # A preload container: the store mounted on /var/lib/docker takes its pulls.
      name=""; store=""
      for ((i = 1; i <= $#; i++)); do
        case "${!i}" in --name) j=$((i + 1)); name="${!j}" ;; -v) j=$((i + 1)); store="${!j}"; store="${store%%:*}" ;; esac
      done
      touch "$S/ctr/$name"
      [ -n "$store" ] && [ -d "$store" ] && touch "$store/pulled-by-preload"
    else
      [ "${VERIFY_FAIL:-0}" = 1 ] && { echo "inner images differ: public.ecr.aws/supabase/postgres:17.6.1"; exit 1; }
    fi ;;
  # docker cp writes host uids into the Sysbox rootfs (the 2026-09-26 preload failure); the build
  # must extract inside the container instead, so a cp here fails the build.
  cp) echo "docker cp would write host uids into the Sysbox rootfs" >&2; exit 1 ;;
  exec)
    if [ "$2" = -i ] && [ "$4" = tar ]; then mkdir -p "$S/cp" && tar -C "$S/cp" -xf -; exit $?; fi
    case "$3" in
      known-ci-entrypoint)
        [ "${PRELOAD_FAIL:-0}" = 1 ] && { echo "supabase start failed in $PRELOAD_DIR_FAIL"; exit 70; }
        printf 'Pulling public.ecr.aws/supabase/postgres:17.6.1\n[runner] preloaded 2 images\n[runner] inner store on disk: 3.9G\n' ;;
      # The build writes an empty manifest when it preloads nothing; the cat then reads it back.
      sh) touch "$S/empty-manifest-$2" ;;
      cat) [ -f "$S/empty-manifest-$2" ] || printf 'public.ecr.aws/supabase/gotrue:v2.180.0\npublic.ecr.aws/supabase/postgres:17.6.1\n' ;;
    esac ;;
  commit)
    [ "${COMMIT_FAIL:-0}" = 1 ] && exit 1
    entry="$(new_entry)"; echo "$entry" > "$S/tags/$5"
    id="${entry%% *}"; echo "${COMMIT_CMD:-[]}" > "$S/cmd/${id#sha256:}" ;;
  tag) cp "$S/tags/$2" "$S/tags/$3" ;;
  rm) rm -f "$S/ctr/$3" ;;
  builder) [ "${BUILDER_PRUNE_FAILS:-0}" = 1 ] && { echo "error during connect" >&2; exit 1; } ;;
esac
exit 0
SH
  cat > "$p/gh" <<'SH'
#!/bin/bash
echo "gh $* HOME=$HOME GH_CONFIG_DIR=${GH_CONFIG_DIR:-unset} GH_TOKEN=${GH_TOKEN:-unset}" >> "$CALLLOG"
path="${*: -1}"; repo="${path#repos/}"; repo="${repo%%/contents/*}"
[ "$repo" = "${GH_FAIL_REPO:-}" ] && { echo "HTTP 404: Not Found" >&2; exit 1; }
cat "$FIXTURES/${repo#*/}.toml"
SH
  cat > "$p/runuser" <<'SH'
#!/bin/bash
echo "runuser $1 $2" >> "$CALLLOG"
shift 3
exec "$@"
SH
  cat > "$p/timeout" <<'SH'
#!/bin/bash
echo "timeout $1" >> "$CALLLOG"
shift
exec "$@"
SH
  # cp: a --reflink=always call is logged and, unless CP_NOREFLINK=1 (a filesystem without
  # reflinks), done as a plain copy; every other cp is the real one.
  cat > "$p/cp" <<'SH'
#!/bin/bash
args=(); reflink=0
for a in "$@"; do case "$a" in --reflink=always) reflink=1 ;; *) args+=("$a") ;; esac; done
if [ "$reflink" = 1 ]; then
  echo "cp --reflink=always ${args[*]}" >> "$CALLLOG"
  [ "${CP_NOREFLINK:-0}" = 1 ] && { echo "cp: failed to clone: Operation not supported" >&2; exit 1; }
fi
exec /bin/cp "${args[@]}"
SH
  chmod +x "$p/"*
}

run_build() {
  env PATH="$TMP/s/pathbin:$PATH" GH_TOKEN="ambient-token-must-not-be-used" \
    RUNNERS_ALLOW_NON_ROOT="${ALLOW_NON_ROOT:-1}" \
    RUNNERS_HOST_YML="${HOST_YML_UNDER_TEST:-$TMP/s/host.yml}" \
    RUNNERS_LANE_STATE_DIR="${STATE_UNDER_TEST-$TMP/s/state}" \
    RUNNERS_BUILD_TODAY="${TODAY:-$DAY1}" \
    bash "$TMP/s/bin/build-runner-image.sh" box "${LANE_UNDER_TEST:-box-ci}" "$@" > "$TMP/s/out" 2>&1
}

tags() { for f in "$TMP/s/docker/tags/${IMG:-box-ci-runner}":*; do [ -e "$f" ] && printf '%s\n' "${f##*:}"; done; }
image_env() { cat "$TMP/s/state/image.env" 2>/dev/null; }
dated() { tags | grep -E '^[0-9]{8}-[0-9a-f]{12}$'; }
id_of() { awk '{print $1}' "$TMP/s/docker/tags/${IMG:-box-ci-runner}:$1" 2>/dev/null; }
tagfile() { printf '%s/docker/tags/%s:%s' "$TMP/s" "${IMG:-box-ci-runner}" "$1"; }
built() { grep -qE '^docker (build|commit) ' "$CALLLOG"; }  # the two subcommands, not `docker builder`
in_order() {  # the arguments occur in the call log in this order (as a subsequence)
  local prev=0 n
  for pat in "$@"; do
    n="$(grep -n -F -- "$pat" "$CALLLOG" | cut -d: -f1 | awk -v p="$prev" '$1 > p { print; exit }')"
    [ -n "$n" ] || return 1
    prev="$n"
  done
}

# ---- first build ---------------------------------------------------------------------------
setup
run_build; rc=$?
[ "$rc" -eq 0 ]; expect $? "a first build succeeds"
[ "$(dated | wc -l | tr -d ' ')" = 1 ] && dated | grep -qE "^$DAY1-[0-9a-f]{12}$"; expect $? "the tag is the UTC build date plus 12 hex of the image definition's hash"
T1="$(dated)"
[ -n "$T1" ] && [ "$(id_of current)" = "$(id_of "$T1")" ]; expect $? "current names the new tag"
[ "$(image_env)" = "KNOWN_CI_IMAGE_TAG=$T1" ]; expect $? "image.env names the new tag for the lane's units"
grep -qF "docker image inspect -f {{index .Config.Labels \"ai-fleet.disk-watch\"}} box-ci-runner:$T1" "$CALLLOG"; expect $? "the built image is checked for the disk-watch keep label"
{ ! tags | grep -q '^base-'; } && [ -z "$(ls "$TMP/s/docker/ctr")" ]; expect $? "no base-* tag and no build container are left behind"
STORE="$TMP/s/state/store"
in_order "docker info" "gh api" "docker build" "docker run -d --runtime=sysbox-runc -v $STORE/golden-$T1.new:/var/lib/docker --name box-ci-runner-build-$T1 box-ci-runner:base-$T1 hold" \
  "docker exec -i box-ci-runner-build-$T1 tar" "docker exec box-ci-runner-build-$T1 known-ci-entrypoint preload" \
  "docker commit --change CMD [] box-ci-runner-build-$T1 box-ci-runner:$T1" "docker rm -f box-ci-runner-build-$T1" \
  "lane-slot snapshot verify-" "docker run --rm --runtime=sysbox-runc -v $STORE/verify-" "lane-slot discard verify-" \
  "docker tag box-ci-runner:$T1 box-ci-runner:current"
expect $? "sequence: configs read, base built, preloaded under sysbox-runc into the new store, committed, verified on a snapshot of the store, then current moved"
[ -f "$STORE/golden-$T1/pulled-by-preload" ] && [ ! -e "$STORE/golden-$T1.new" ] && ! compgen -G "$STORE/verify-*" >/dev/null; expect $? "the preloaded store lands at <store>/golden-<tag> and the verify snapshot is removed"
grep -qE "^lane-slot snapshot verify-[0-9]+ store=$STORE tag=$T1$" "$CALLLOG" && grep -qE "^docker run --rm --runtime=sysbox-runc -v $STORE/verify-[0-9]+:/var/lib/docker box-ci-runner:$T1 verify-preload$" "$CALLLOG"; expect $? "verify-preload runs on a snapshot the slot helper made from the store, the way a job gets its store"
grep -q "^cp --reflink=always $STORE/.reflink-probe" "$CALLLOG"; expect $? "the build probes that the store takes reflinks before doing anything"
grep -q "store -> $STORE/golden-$T1" "$TMP/s/out"; expect $? "the log names the new store"
grep -qx "docker build --platform linux/amd64 --progress=plain -t box-ci-runner:base-$T1 $TMP/s/image" "$CALLLOG"; expect $? "the base image is built for linux/amd64 from image/ with the host's default runtime"
grep -qx "docker exec box-ci-runner-build-$T1 known-ci-entrypoint preload $ROOTS" "$CALLLOG"; expect $? "preload runs once per preloaded repository, each at /opt/known-ci/preload/<name>"
grep -qx "docker exec -i box-ci-runner-build-$T1 tar -C $PRELOAD -xf -" "$CALLLOG"; expect $? "the staged projects are extracted inside the build container as its own root (docker cp would leave host uids the Sysbox userns cannot write)"
{ ! grep -q "^docker exec box-ci-runner-build-$T1 rm " "$CALLLOG"; }; expect $? "the preload projects stay in the image for the lane's zero-pull proof"
[ "$(grep -c '^runuser -u box-ci$' "$CALLLOG")" = 5 ] && [ "$(grep -c "^gh api -H Accept: application/vnd.github.raw repos/acme/.*/contents/.*config.toml HOME=/var/lib/box-ci/home GH_CONFIG_DIR=/var/lib/box-ci/home/.config/gh GH_TOKEN=unset$" "$CALLLOG")" = 5 ]
expect $? "each config.toml is read with gh as the lane's own user, on its own session, raw, through the contents API, never an ambient token"
{ ! grep -q "repos/acme/tools/" "$CALLLOG"; }; expect $? "a repository marked \"-\" (no Supabase project) is not read"
grep -q "^gh api .* repos/acme/beta/contents/packages/database/supabase/config.toml " "$CALLLOG"; expect $? "a nested supabase directory is read at its own path"
cmp -s "$TMP/s/fixtures/beta.toml" "$TMP/s/docker/cp/beta/supabase/config.toml" && cmp -s "$TMP/s/fixtures/alpha.toml" "$TMP/s/docker/cp/alpha/supabase/config.toml"; expect $? "the staged tree holds each repository's config.toml unchanged"
[ -f "$TMP/s/docker/cp/omega/supabase/templates/invite.html" ] && [ ! -s "$TMP/s/docker/cp/omega/supabase/templates/invite.html" ] \
  && [ -f "$TMP/s/docker/cp/omega/supabase/templates/password_changed.html" ]; expect $? "empty stand-ins exist where the CLI resolves each email template"
[ "$(find "$TMP/s/docker/cp" -type f | wc -l | tr -d ' ')" = 7 ]; expect $? "nothing but the five configs and the two stand-ins is staged"
grep -q "preloaded public.ecr.aws/supabase/postgres:17.6.1" "$TMP/s/out" && grep -q "current -> box-ci-runner:$T1" "$TMP/s/out"; expect $? "the log lists the preloaded images and the new current"
grep -q "preload: inner store on disk: 3.9G" "$TMP/s/out" && ! grep -q "Pulling" "$TMP/s/out"; expect $? "the log keeps the preload's summary (inner store size) and drops its pull progress"

# ---- converging ------------------------------------------------------------------------------
: > "$CALLLOG"
run_build; rc=$?
[ "$rc" -eq 0 ] && grep -q "converged: current is box-ci-runner:$T1" "$TMP/s/out" && ! built && ! grep -q '^gh ' "$CALLLOG"; expect $? "an unchanged definition converges without reading GitHub or building"
rm -f "$TMP/s/state/image.env"; : > "$CALLLOG"
run_build; rc=$?
[ "$rc" -eq 0 ] && [ "$(image_env)" = "KNOWN_CI_IMAGE_TAG=$T1" ] && ! built; expect $? "a converged run restores a missing image.env without building"
printf '\nedited prose\n' >> "$CTX/README.md"; : > "$CALLLOG"
run_build; rc=$?
[ "$rc" -eq 0 ] && ! built; expect $? "a README edit is not a new image definition"
rm -rf "$STORE/golden-$T1"; : > "$CALLLOG"
run_build; rc=$?
[ "$rc" -eq 0 ] && built && grep -q "box-ci-runner:$T1 has no store at $STORE/golden-$T1; untagging it and building again" "$TMP/s/out" \
  && grep -qx "docker image rm box-ci-runner:$T1" "$CALLLOG" && [ -f "$STORE/golden-$T1/pulled-by-preload" ] && [ "$(id_of current)" = "$(id_of "$T1")" ]
expect $? "an image whose store is gone is not converged on: it is untagged and built again, store included"
printf '# a pin bump\n' >> "$CTX/entrypoint.sh"; : > "$CALLLOG"
run_build; rc=$?
T2="$(dated | grep -vx "$T1")"
[ "$rc" -eq 0 ] && [ -n "$T2" ] && [ "${T2%%-*}" = "$DAY1" ] && [ "${T2#*-}" != "${T1#*-}" ]; expect $? "a changed image file yields a new hash, so a new tag, the same day"
[ "$(id_of current)" = "$(id_of "$T2")" ] && [ "$(image_env)" = "KNOWN_CI_IMAGE_TAG=$T2" ] && [ -f "$(tagfile "$T1")" ]; expect $? "current and image.env move to the rebuilt image and the previous one is kept"
cp "$ROOT/image/entrypoint.sh" "$CTX/entrypoint.sh"; : > "$CALLLOG"
run_build; rc=$?
[ "$rc" -eq 0 ] && [ "$(id_of current)" = "$(id_of "$T1")" ] && [ "$(image_env)" = "KNOWN_CI_IMAGE_TAG=$T1" ] && ! built && grep -q "already built and verified" "$TMP/s/out"; expect $? "reverting the definition points current and image.env back at its verified image without a rebuild"
: > "$CALLLOG"
TODAY=20261002 run_build --refresh; rc=$?
[ "$rc" -eq 0 ] && [ -f "$(tagfile "20261002-${T1#*-}")" ] && [ "$(id_of current)" = "$(id_of "20261002-${T1#*-}")" ]; expect $? "--refresh on a new day builds <today>-<hash> and moves current"
: > "$CALLLOG"
TODAY=20261002 run_build --refresh; rc=$?
[ "$rc" -eq 0 ] && ! built; expect $? "--refresh again the same day converges"

# ---- pruning -----------------------------------------------------------------------------------
TODAY=20261009 run_build --refresh; TODAY=20261016 run_build --refresh; rc=$?
[ "$rc" -eq 0 ] && [ "$(dated | sort | tr '\n' ' ')" = "20261002-${T1#*-} 20261009-${T1#*-} 20261016-${T1#*-} " ]; expect $? "only the newest three dated tags are kept"
grep -q "pruned box-ci-runner:$T1" "$TMP/s/out" || grep -q "pruned box-ci-runner:$T2" "$TMP/s/out"; expect $? "pruning is logged"
[ "$(ls "$STORE" | sort | tr '\n' ' ')" = "golden-20261002-${T1#*-} golden-20261009-${T1#*-} golden-20261016-${T1#*-} " ] && { grep -q "pruned store $STORE/golden-$T1" "$TMP/s/out" || grep -q "pruned store $STORE/golden-$T2" "$TMP/s/out"; }; expect $? "a pruned tag's store goes with it, and the kept tags keep theirs"
mkdir -p "$STORE/golden-20250101-cccccccccccc" "$STORE/golden-20261016-${T1#*-}.new"; : > "$CALLLOG"
TODAY=20261016 run_build --refresh; rc=$?
[ "$rc" -eq 0 ] && ! built && [ ! -e "$STORE/golden-20250101-cccccccccccc" ] && [ ! -e "$STORE/golden-20261016-${T1#*-}.new" ] && [ -d "$STORE/golden-20261016-${T1#*-}" ]; expect $? "a store without an image and an unfinished store are pruned; the current one stays"
in_order "docker image ls" "docker builder prune --force --filter until=168h" && grep -q "pruned build cache unused for 168h" "$TMP/s/out"; expect $? "after the tags, the build drops Docker build-cache records unused for a week, so nothing else has to"
[ "$(grep -c '^docker builder prune' "$CALLLOG")" = 1 ] && ! grep -qE '^docker (image|system|volume|container) prune' "$CALLLOG"; expect $? "that is the only prune it runs: no image, volume, container or system prune of the machine"
: > "$CALLLOG"
BUILDER_PRUNE_FAILS=1 TODAY=20261016 run_build --refresh; rc=$?
[ "$rc" -eq 0 ] && grep -q "WARN could not prune the Docker build cache" "$TMP/s/out" && [ "$(id_of current)" = "$(id_of "20261016-${T1#*-}")" ]; expect $? "a build-cache prune that fails is a warning: the run succeeds and current stays"
# An image built before the store existed is among the newest three but no slot can run it:
# prepare fails closed without a store. It goes at the next build.
printf 'sha256:%064d 2026-12-31T00:00:00Z\n' 77 > "$(tagfile 20261231-eeeeeeeeeeee)"
rm -rf "$STORE/golden-20261009-${T1#*-}"; : > "$CALLLOG"
TODAY=20261016 run_build --refresh; rc=$?
[ "$rc" -eq 0 ] && ! built && [ ! -f "$(tagfile 20261231-eeeeeeeeeeee)" ] && [ ! -f "$(tagfile "20261009-${T1#*-}")" ] \
  && [ -f "$(tagfile "20261002-${T1#*-}")" ] && [ -f "$(tagfile "20261016-${T1#*-}")" ] && [ "$(id_of current)" = "$(id_of "20261016-${T1#*-}")" ] \
  && grep -q "pruned box-ci-runner:20261231-eeeeeeeeeeee" "$TMP/s/out" && grep -q "pruned box-ci-runner:20261009-${T1#*-}" "$TMP/s/out"
expect $? "a dated tag without its store is pruned however new it is, does not count toward the kept three, and the ones with stores stay"
# Synthetic tags older than any build: 2025-12-0N. The current definition's hash comes from a real
# first build, so the oldest tag can carry it and be what current names.
setup
run_build; H="$(dated)"; H="${H#*-}"
rm -f "$TMP/s/docker/tags/"*
printf 'sha256:%064d 2025-12-01T00:00:00Z\n' 91 > "$(tagfile "20250901-$H")"
for n in 2 3 4 5; do printf 'sha256:%064d 2025-12-0%dT00:00:00Z\n' "9$n" "$n" > "$(tagfile "2025090$n-aaaaaaaaaaaa")"; mkdir "$STORE/golden-2025090$n-aaaaaaaaaaaa"; done
cp "$(tagfile "20250901-$H")" "$(tagfile current)"
mkdir "$STORE/golden-20250901-$H"
: > "$CALLLOG"
run_build; rc=$?
[ "$rc" -eq 0 ] && ! built && [ -f "$(tagfile "20250901-$H")" ] && [ ! -f "$(tagfile 20250902-aaaaaaaaaaaa)" ] \
  && [ -f "$(tagfile 20250903-aaaaaaaaaaaa)" ]; expect $? "pruning keeps the tag current names even when it is not among the newest three"
[ -d "$STORE/golden-20250901-$H" ] && [ ! -e "$STORE/golden-$DAY1-$H" ]; expect $? "its store is kept too, and the first build's store, whose tag is gone, is pruned"
setup
for n in 1 2 3 4; do printf 'sha256:%064d 2025-12-0%dT00:00:00Z\n' "9$n" "$n" > "$(tagfile "2025090$n-bbbbbbbbbbbb")"; done
: > "$CALLLOG"
IN_USE="20250901-bbbbbbbbbbbb" run_build; rc=$?
[ "$rc" -eq 0 ] && grep -q "WARN could not remove box-ci-runner:20250901-bbbbbbbbbbbb" "$TMP/s/out" \
  && [ -f "$(tagfile 20250901-bbbbbbbbbbbb)" ] && [ ! -f "$(tagfile 20250902-bbbbbbbbbbbb)" ]; expect $? "an old tag a container still uses is kept with a warning, and pruning goes on"

# ---- failures leave current where it was ---------------------------------------------------
fresh_definition() { setup; run_build; BEFORE="$(id_of current)"; OLD="$(dated)"; ENV_BEFORE="$(image_env)"; printf '# change\n' >> "$CTX/daemon.json.note"; : > "$CALLLOG"; }
fresh_definition
PRELOAD_FAIL=1 PRELOAD_DIR_FAIL="$PRELOAD/omega" run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "preload failed" "$TMP/s/out" && grep -q "supabase start failed in $PRELOAD/omega" "$TMP/s/out"; expect $? "a failed preload fails the build and shows the preload's own error"
[ "$(id_of current)" = "$BEFORE" ] && [ "$(image_env)" = "$ENV_BEFORE" ] && [ "$(dated)" = "$OLD" ] && { ! tags | grep -q '^base-'; } && [ -z "$(ls "$TMP/s/docker/ctr")" ] && ! grep -q '^docker commit' "$CALLLOG"; expect $? "after a failed preload: current and image.env unchanged, no new or base tag, build container removed, nothing committed"
[ "$(ls "$STORE" | tr '\n' ' ')" = "golden-$OLD " ]; expect $? "after a failed preload: the unfinished store is removed"
fresh_definition
VERIFY_FAIL=1 run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "verify failed" "$TMP/s/out" && [ "$(id_of current)" = "$BEFORE" ] && [ "$(image_env)" = "$ENV_BEFORE" ] && [ "$(dated)" = "$OLD" ]; expect $? "a committed image that fails verify-preload is deleted; current and image.env stay"
[ "$(ls "$STORE" | tr '\n' ' ')" = "golden-$OLD " ]; expect $? "after a failed verify: its store and verify snapshot are gone, the previous store stays"
fresh_definition
COMMIT_CMD='["hold"]' run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q 'kept CMD \["hold"\]' "$TMP/s/out" && ! grep -q 'verify-preload' "$CALLLOG" && [ "$(id_of current)" = "$BEFORE" ] && [ "$(dated)" = "$OLD" ]; expect $? "a commit that kept CMD hold is deleted before verify, and current stays"
fresh_definition
STUB_ENTRYPOINT='["/bin/sh"]' run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q 'has ENTRYPOINT \["/bin/sh"\]' "$TMP/s/out" && [ "$(id_of current)" = "$BEFORE" ] && [ "$(dated)" = "$OLD" ]; expect $? "a commit with the wrong ENTRYPOINT is deleted and current stays"
fresh_definition
STUB_KEEP_LABEL='' run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q 'lacks the label ai-fleet.disk-watch=keep' "$TMP/s/out" && [ "$(id_of current)" = "$BEFORE" ] && [ "$(image_env)" = "$ENV_BEFORE" ] && [ "$(dated)" = "$OLD" ]; expect $? "a commit without the disk-watch keep label is deleted and current stays"
grep -qx 'LABEL ai-fleet.disk-watch=keep' "$ROOT/image/Dockerfile"; expect $? "the image definition carries the legacy disk-watch keep label"
fresh_definition
BUILD_FAIL=1 run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "build failed" "$TMP/s/out" && grep -q "failed to solve" "$TMP/s/out" && ! grep -q '^docker run' "$CALLLOG" && [ "$(id_of current)" = "$BEFORE" ]; expect $? "a failed docker build shows its error, runs nothing, and current stays"
fresh_definition
GH_FAIL_REPO=acme/omega run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "cannot read acme/omega supabase/config.toml with gh as box-ci" "$TMP/s/out" && ! grep -q '^docker build' "$CALLLOG" && [ "$(id_of current)" = "$BEFORE" ]; expect $? "an unreadable config.toml stops the run before any build, naming the repository"

# ---- preconditions and refusals ------------------------------------------------------------
setup
LANE_UNDER_TEST=nope run_build --if-provisioned; rc=$?
[ "$rc" -ne 0 ] && grep -q "cannot read the nope lane from" "$TMP/s/out" && ! grep -qE '^(gh|docker)' "$CALLLOG"; expect $? "a lane the host config does not declare fails, even from the timer, before reading GitHub or Docker"
# The image name is the lane's: one definition serves every lane, each under its own image name.
setup
host_variant "$TMP/s/host-otherimage.yml" 'lanes["box-ci"]["image"] = "other-runner"'
HOST_YML_UNDER_TEST="$TMP/s/host-otherimage.yml" run_build; rc=$?
OT="$(IMG=other-runner dated)"
[ "$rc" -eq 0 ] && grep -q "^docker build --platform linux/amd64 --progress=plain -t other-runner:base-$OT " "$CALLLOG" \
  && grep -qx "docker tag other-runner:$OT other-runner:current" "$CALLLOG" && ! grep -q "box-ci-runner:" "$CALLLOG"
expect $? "the image built, tagged and made current is the one the lane names"
setup
NO_SYSBOX=1 run_build --if-provisioned; rc=$?
[ "$rc" -eq 0 ] && grep -q "no sysbox-runc runtime on this host: the box-ci lane is not provisioned here" "$TMP/s/out" && grep -q "nothing to build" "$TMP/s/out" && ! built && ! grep -q '^gh ' "$CALLLOG"; expect $? "--if-provisioned on a host without sysbox-runc is a logged no-op"
NO_SYSBOX=1 run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "no sysbox-runc runtime" "$TMP/s/out" && ! built; expect $? "without --if-provisioned a missing sysbox-runc runtime is a failure"
DOCKER_DOWN=1 run_build --if-provisioned; rc=$?
[ "$rc" -ne 0 ] && grep -q "docker info failed" "$TMP/s/out"; expect $? "an unreachable Docker daemon is a failure even with --if-provisioned"
rm -rf "$TMP/s/state/store"; : > "$CALLLOG"
run_build --if-provisioned; rc=$?
[ "$rc" -eq 0 ] && grep -q "no reflink-capable store at $TMP/s/state/store" "$TMP/s/out" && grep -q "nothing to build" "$TMP/s/out" && ! built && ! grep -q '^gh ' "$CALLLOG"; expect $? "--if-provisioned without the store filesystem is a logged no-op"
run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "no reflink-capable store" "$TMP/s/out" && ! built; expect $? "without --if-provisioned a missing store is a failure"
mkdir -p "$TMP/s/state/store"
CP_NOREFLINK=1 run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "no reflink-capable store" "$TMP/s/out" && ! built && [ -z "$(ls -A "$TMP/s/state/store")" ]; expect $? "a store directory that takes no reflinks (not the XFS image) is refused, and the probe files are removed"
setup
printf '[auth.email.template.invite]\ncontent_path = "../../etc/passwd"\n' > "$TMP/s/fixtures/omega.toml"
run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "leaves the repository" "$TMP/s/out" && ! built; expect $? "a template path outside the repository gets no stand-in and stops the run"
printf '[auth.email.template.invite]\ncontent_path = "/etc/passwd"\n' > "$TMP/s/fixtures/omega.toml"
run_build; rc=$?
[ "$rc" -ne 0 ] && grep -q "is absolute" "$TMP/s/out" && ! built; expect $? "an absolute template path stops the run"
setup
run_build --bogus; rc=$?
[ "$rc" -ne 0 ] && grep -q "unknown argument: --bogus" "$TMP/s/out"; expect $? "an unknown argument is refused"
out="$(env PATH="$TMP/s/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 bash "$TMP/s/bin/build-runner-image.sh" box 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "usage:" <<< "$out"; expect $? "a missing lane argument is refused with the usage"
if [ "$(id -u)" != 0 ]; then
  ALLOW_NON_ROOT=0 run_build; rc=$?
  [ "$rc" -ne 0 ] && grep -q "must run as root" "$TMP/s/out" && [ ! -s "$CALLLOG" ]; expect $? "a non-root caller is refused before touching Docker"
fi

# ---- the preload map is the lane's own ------------------------------------------------------------
# The user lane: its map is its list, its configs are read at repos/<owner>/<repo>, as its own user,
# and its image and state are named for the lane.
setup
host_variant "$TMP/s/host-epsilon.yml" 'lanes["own-ci"]["preload"] = {"epsilon": "supabase"}'
LANE_UNDER_TEST=own-ci HOST_YML_UNDER_TEST="$TMP/s/host-epsilon.yml" run_build; rc=$?
GT="$(IMG=own-ci-runner dated)"
[ "$rc" -eq 0 ] && [ -n "$GT" ]; expect $? "a user lane's image builds from its own preload map"
[ "$(grep -c '^gh ' "$CALLLOG")" = 1 ] \
  && grep -qx "gh api -H Accept: application/vnd.github.raw repos/solo/epsilon/contents/supabase/config.toml HOME=/var/lib/own-ci/home GH_CONFIG_DIR=/var/lib/own-ci/home/.config/gh GH_TOKEN=unset" "$CALLLOG" \
  && grep -qx "runuser -u own-ci" "$CALLLOG"
expect $? "the user lane reads each preloaded repository's config at repos/<owner>/<repo>/contents/..., with gh as the lane's user and home"
grep -qx "docker exec own-ci-runner-build-$GT known-ci-entrypoint preload $PRELOAD/epsilon" "$CALLLOG"; expect $? "the user lane preloads exactly its map's repositories"
grep -q "^docker build --platform linux/amd64 --progress=plain -t own-ci-runner:base-$GT $TMP/s/image$" "$CALLLOG" \
  && grep -qx "docker tag own-ci-runner:$GT own-ci-runner:current" "$CALLLOG" && [ "$(image_env)" = "KNOWN_CI_IMAGE_TAG=$GT" ]
expect $? "the user lane's image is its own (own-ci-runner) from the shared definition"
{ ! grep -q 'repos/acme/' "$CALLLOG"; }; expect $? "the user lane never reads another lane's repositories"
# An empty map preloads nothing and still yields a current image.
setup
LANE_UNDER_TEST=own-ci run_build; rc=$?
ET="$(IMG=own-ci-runner dated)"
[ "$rc" -eq 0 ] && [ -n "$ET" ] && grep -qxF "docker commit --change CMD [] own-ci-runner-build-$ET own-ci-runner:$ET" "$CALLLOG" \
  && grep -qxF "docker tag own-ci-runner:$ET own-ci-runner:current" "$CALLLOG" && [ "$(image_env)" = "KNOWN_CI_IMAGE_TAG=$ET" ]
expect $? "an empty preload map still builds, commits, tags and makes current the lane's image"
[ -d "$STORE/golden-$ET" ] && [ ! -e "$STORE/golden-$ET.new" ] && grep -q "store -> $STORE/golden-$ET" "$TMP/s/out"; expect $? "its golden store is still created at <store>/golden-<tag>"
{ ! grep -qE '^(gh|runuser) ' "$CALLLOG"; } && ! grep -q "known-ci-entrypoint preload" "$CALLLOG" && ! grep -qE "^docker exec -i .* tar " "$CALLLOG" \
  && grep -q "no repository to preload: the own-ci lane's preload map lists no Supabase project" "$TMP/s/out"
expect $? "an empty map reads no config, copies nothing, runs no preload, and says so"
in_order "docker run -d --runtime=sysbox-runc -v $STORE/golden-$ET.new:/var/lib/docker --name own-ci-runner-build-$ET own-ci-runner:base-$ET hold" \
  "docker exec own-ci-runner-build-$ET sh -c mkdir -p /etc/known-ci-runner && : > /etc/known-ci-runner/preloaded-images" \
  "docker exec own-ci-runner-build-$ET cat /etc/known-ci-runner/preloaded-images" "docker commit --change CMD [] own-ci-runner-build-$ET"
expect $? "the build container records an empty manifest before the commit, so the image says it preloaded nothing"
{ ! grep -qE "verify-preload|^lane-slot " "$CALLLOG"; } && grep -q "nothing preloaded, so no verify-preload" "$TMP/s/out" && ! grep -q "preloaded public.ecr.aws" "$TMP/s/out"
expect $? "an empty map runs no verify-preload and lists no preloaded image, and the log says why"
setup
LANE_UNDER_TEST=own-ci STATE_UNDER_TEST="" run_build --if-provisioned; rc=$?
[ "$rc" -eq 0 ] && grep -q "no reflink-capable store at /var/lib/own-ci/store: the own-ci lane's store filesystem" "$TMP/s/out"
expect $? "without an override the state and store paths derive from the lane name (/var/lib/<lane>)"
# The image-build timer runs the build with no environment of its own: the host file is the values
# checkout's hosts/<host>.yml.
setup
mkdir -p "$TMP/s/config/hosts"; cp "$TMP/s/host.yml" "$TMP/s/config/hosts/box.yml"
run_from_values() {  # <host> <lane> args
  env PATH="$TMP/s/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 RUNNERS_CONFIG="$TMP/s/config" RUNNERS_BUILD_TODAY="$DAY1" \
    bash "$TMP/s/bin/build-runner-image.sh" "$@" > "$TMP/s/out" 2>&1
}
run_from_values box own-ci --if-provisioned; rc=$?
[ "$rc" -eq 0 ] && grep -q "no reflink-capable store at /var/lib/own-ci/store: the own-ci lane's store filesystem" "$TMP/s/out"
expect $? "with no host file named outright, the lane is read from hosts/<host>.yml of the values checkout"
run_from_values nowhere own-ci --if-provisioned; rc=$?
[ "$rc" -ne 0 ] && grep -q "cannot read the own-ci lane from $TMP/s/config/hosts/nowhere.yml" "$TMP/s/out" && ! built
expect $? "and a host that checkout has no file for stops the build, naming the path"
grep -qx 'HOST_YML="${RUNNERS_HOST_YML:-${RUNNERS_CONFIG:-/opt/runner-lanes-config}/hosts/$HOST.yml}"' "$ROOT/bin/build-runner-image.sh"; expect $? "on a machine that checkout is /opt/runner-lanes-config"

# The tag hashes image/ and this lane's own preload map, never another lane's.
setup
run_build; T1="$(dated)"
host_variant "$TMP/s/host-other-map.yml" 'lanes["own-ci"]["preload"] = {"epsilon": "supabase"}'
: > "$CALLLOG"
HOST_YML_UNDER_TEST="$TMP/s/host-other-map.yml" run_build; rc=$?
[ "$rc" -eq 0 ] && ! built && grep -q "converged: current is box-ci-runner:$T1" "$TMP/s/out"; expect $? "another lane's preload map is not part of this lane's image definition"
host_variant "$TMP/s/host-own-map.yml" 'lanes["box-ci"]["preload"]["beta"] = "supabase"'
: > "$CALLLOG"
HOST_YML_UNDER_TEST="$TMP/s/host-own-map.yml" run_build; rc=$?
T3="$(dated | grep -vx "$T1")"
[ "$rc" -eq 0 ] && built && [ -n "$T3" ] && [ "${T3#*-}" != "${T1#*-}" ]; expect $? "the lane's own preload map is: a changed entry is a new tag"
host_variant "$TMP/s/host-reordered.yml" 'lanes["box-ci"]["preload"] = dict(reversed(list(lanes["box-ci"]["preload"].items())))'
: > "$CALLLOG"
HOST_YML_UNDER_TEST="$TMP/s/host-reordered.yml" run_build; rc=$?
[ "$rc" -eq 0 ] && ! built && grep -q "box-ci-runner:$T1" "$TMP/s/out"; expect $? "the map is hashed in canonical text: reordering its entries is the same definition"

# ---- the image definition against the lane's contract (README.md) ---------------------------
D="$ROOT/image/Dockerfile"
{ ! grep -E '^(COPY|ADD)[^#]* /home/runner' "$D"; } && grep -q 'tar -xzf runner.tgz -C /out/opt/actions-runner' "$D"; expect $? "the runner is installed at /opt/actions-runner and nothing is baked under /home/runner"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("storage-driver") == "overlay2" and d.get("features", {}).get("containerd-snapshotter") is False and "data-root" not in d else 1)' "$ROOT/image/daemon.json"; expect $? "the inner dockerd uses classic overlay2 at its default data root, containerd snapshotter off"
grep -qE '^ENV .*SUPABASE_HOME=/tmp/' "$D"; expect $? "the Supabase CLI's state home is on the writable /tmp"
# The build copies the project configs into /opt/known-ci/preload; the first real build died with
# "Could not find the file /opt/known-ci in container" (2026-09-25).
grep -qE '^[^#]*install -d[^#]* /opt/known-ci/preload( |$)' "$D"; expect $? "the image creates the preload directory the build copies the project configs into"
grep -qx 'ENTRYPOINT \["/usr/local/bin/known-ci-entrypoint"\]' "$D" && grep -q 'ENTRYPOINT_JSON=.\["/usr/local/bin/known-ci-entrypoint"\]' "$ROOT/bin/build-runner-image.sh"; expect $? "the in-image entrypoint path is the one running images and the build agree on"
{ ! ls "$ROOT/image" | grep -q '^supabase-projects'; }; expect $? "no project map lives in image/: each lane's preload map is in its host config"

echo
echo "build-runner-image-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
