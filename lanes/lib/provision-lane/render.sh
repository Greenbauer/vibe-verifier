# shellcheck shell=bash
# render.sh: the units and files the lane's phases write, rendered as text from the lane's settings.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.

render_slice() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Aggregate limits for every $NAME container (docker --cgroup-parent) and slot unit (Slice=):
# whatever the per-container caps, the lane together never takes more than this from the machine.
[Unit]
Description=$NAME lane: aggregate limits for its disposable CI containers
Before=slices.target

[Slice]
MemoryMax=$MEMORY_MAX
MemoryHigh=$MEMORY_HIGH
CPUQuota=$CPU_QUOTA
CPUWeight=$CPU_WEIGHT
EOF
}

render_top_slice() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# systemd nests slices on dashes, so $SLICE lives inside this one. A CPUWeight only competes with
# siblings, so the lane's weight sits here too, where it ranks against system.slice.
[Unit]
Description=Parent of $SLICE
Before=slices.target

[Slice]
CPUWeight=$CPU_WEIGHT
EOF
}

# The store's mount options after `loop`. XFS: discard, so a deleted snapshot gives its blocks back
# to the sparse image. btrfs: noatime, because under the default (relatime) the first read of a
# file in a fresh snapshot updates its access time and so copies its metadata, which btrfs(5) names
# as relatime's worst case (many files, older than a day, read just after a snapshot), and that is
# how every job starts; and discard=async, for the same blocks back to the sparse image: btrfs(5)
# calls it the preferred mode, gathering freed extents into larger chunks before the TRIM, where
# the synchronous mode (a plain discard) can degrade performance.
store_mount_options() { if [ "$1" = btrfs ]; then printf 'noatime,discard=async'; else printf 'discard'; fi; }

# The btrfs unit's text still names bin/provision-lane.sh, where store_mount_options was before the
# script was split: new text would rewrite every btrfs lane's mount unit on its next pull.
render_store_mount() {  # $1: the store's filesystem, xfs or btrfs
  local what
  if [ "$1" = btrfs ]; then
    what="# The lane's inner Docker store: golden-<tag> (the image build's preload, a subvolume) and one
# snapshot of it per running job, mounted on that job's /var/lib/docker. btrfs for the snapshots;
# the options are explained at store_mount_options in bin/provision-lane.sh."
  else
    what="# The lane's inner Docker store: golden-<tag> (the image build's preload) and one reflink snapshot
# of it per running job, mounted on that job's /var/lib/docker. XFS for the reflinks; discard so a
# deleted snapshot gives its blocks back to the sparse image."
  fi
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
$what
[Unit]
Description=Preloaded inner Docker store and per-job snapshots ($NAME lane)
RequiresMountsFor=$STATE_DIR

[Mount]
What=$STORE_IMG
Where=$STORE_DIR
Type=$1
Options=loop,$(store_mount_options "$1")

[Install]
WantedBy=multi-user.target
EOF
}

# The docker run flags a slot uses; --check's smoke container reuses them, minus the slot mounts.
# No --read-only: Sysbox mounts its per-container /var/lib/docker read-only when the rootfs is,
# so the inner dockerd dies ("chmod /var/lib/docker: read-only file system", 2026-09-26). The job
# runs as uid 1001 without sudo, so /etc, /usr, /opt, /var and /root still refuse its writes, and the
# rootfs is discarded with the container (--rm). /tmp is executable like a hosted runner's: Docker's
# tmpfs default is noexec, and a job's stub scripts there failed with "permission denied"
# (2026-09-28). --oom-score-adj 1000 makes the runner's own processes the kernel's first choice
# under memory pressure; Sysbox lets a container lower its own score again, so the slice's hard cap,
# not this flag, is what protects the rest of the machine.
# The memory is the lane's container.memory unless a kind's is passed (the wait kind's wait.memory).
container_flags() {
  printf '%s ' --runtime=sysbox-runc --network "$NAME" --cgroup-parent "$SLICE" \
    --memory "${1:-$CONTAINER_MEMORY}" --pids-limit "$PIDS" --tmpfs "/tmp:size=$TMP_SIZE,exec" \
    --oom-score-adj 1000 --dns 1.1.1.1 --dns 8.8.8.8
}

# A kind's instance count and container memory: the wait kind's own (the lane's wait block, a budget
# apart from `slots`), the lane's for every other kind.
kind_slots() { if [ "$1" = wait ]; then printf '%s' "$WAIT_SLOTS"; else printf '%s' "$SLOTS"; fi; }
kind_memory() { if [ "$1" = wait ]; then printf '%s' "$WAIT_MEMORY"; else printf '%s' "$CONTAINER_MEMORY"; fi; }

# A job sees CPUS_PER_JOB cores, like a GitHub-hosted ubuntu runner's 4 vCPU: without a cpuset every
# container reports the host's cores, and a jest run started 15 workers inside a 4 GB container and
# was killed (2026-09-28). Instance n of each kind is pinned to the n-th 4-core set, wrapping round
# the host's cores; a quota alone would not change what nproc reports.
CPUS_PER_JOB=4
host_cpus() { nproc; }
slot_cpuset() {
  local slot="$1" total start
  total="$(host_cpus)"
  if [ "$total" -le "$CPUS_PER_JOB" ]; then printf '0-%s' "$((total - 1))"; return; fi
  start=$(( ((slot - 1) * CPUS_PER_JOB) % total ))
  [ $((start + CPUS_PER_JOB)) -le "$total" ] || start=$((total - CPUS_PER_JOB))
  printf '%s-%s' "$start" "$((start + CPUS_PER_JOB - 1))"
}

render_cpusets() {
  local n
  for n in $(seq 1 "$1"); do printf 'Environment=KNOWN_CI_CPUSET_%s=%s\n' "$n" "$(slot_cpuset "$n")"; done
}

template_of() { printf '%s-%s@.service' "$NAME" "$1"; }

# Each qae instance's own Codex login store: instance n mounts KNOWN_CI_CODEX_STORE_<n>.
render_codex_stores() {
  local n=0 store
  for store in $CODEX_STORES; do
    n=$((n + 1))
    printf 'Environment=KNOWN_CI_CODEX_STORE_%s=%s\n' "$n" "$(codex_store_dir "$store")"
  done
}

render_unit() {
  local kind="$1" store_env="" store_mount="" store_lock=""
  if [ "$kind" = qae ]; then
    store_env="# One Codex login store per qae instance, mounted into that instance's QAE jobs only: Codex
# rotates a login's refresh token as a job uses it, so two concurrent jobs on one store would retire
# each other's session. systemd expands %i first, then \${KNOWN_CI_CODEX_STORE_<n>}; an instance
# beyond qae_concurrency has no store, and its prepare refuses. prepare resets the store to the
# login and the tracked config before every job, and cleanup gives it back to $LANE_USER.
# The job holds the store's lock, $RUN_DIR/qae/<n>.codex.lock, for its whole run, as prepare and
# cleanup do while they touch the store, so $KEEPALIVE_SERVICE never refreshes a login a job is
# using (bin/lane-slot.sh, \"The Codex store lock\"). flock -F executes docker run in its own place,
# so the unit's main process and exit status stay docker run's; it waits at most 60 s, though only
# the keepalive checking the instance (a moment) can hold the lock then.
$(render_codex_stores)
Environment=KNOWN_CI_CODEX_CONFIG=$CODEX_CONFIG
Environment=KNOWN_CI_CODEX_OWNER=$LANE_USER
"
    store_mount="-v \${KNOWN_CI_CODEX_STORE_%i}:/home/runner/.codex-qae "
    store_lock="/usr/bin/flock -F -w 60 $RUN_DIR/qae/%i.codex.lock "
  fi
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# One $kind slot of the $NAME lane. The lane's listener ($LISTENER_UNIT) writes the slot's JIT
# config and job file under $RUN_DIR/$kind/<n>/ and starts this unit for that one job; the
# container takes the job and exits, and the unit stays down until the listener starts it again.
# No [Install]: an instance started at boot would have no job to run. See README.md.
[Unit]
Description=$NAME lane $kind slot %i (one disposable Sysbox container per job)
Requires=docker.service sysbox.service
After=docker.service sysbox.service network-online.target
Wants=network-online.target
RequiresMountsFor=$STATE_DIR $STORE_DIR
# Never refuse a start: the listener may start one instance several times a minute.
StartLimitIntervalSec=0

[Service]
Type=simple
Slice=$SLICE
Environment=KNOWN_CI_NAME=$NAME
Environment=KNOWN_CI_SCOPE=$SCOPE
Environment=KNOWN_CI_SLOT_GB=$SLOT_GB
Environment=KNOWN_CI_BRIDGE=$BRIDGE
Environment=KNOWN_CI_GH_HOSTS=$GH_HOSTS
Environment=KNOWN_CI_STATE_DIR=$STATE_DIR
Environment=KNOWN_CI_STORE_DIR=$STORE_DIR
Environment=KNOWN_CI_RUN_DIR=$RUN_DIR
${store_env}# Each instance's 4-core set (systemd expands %i first, then \${KNOWN_CI_CPUSET_<n>}), so a job
# sees nproc=$CPUS_PER_JOB like a hosted runner.
$(render_cpusets "$(kind_slots "$kind")")
# KNOWN_CI_IMAGE_TAG, written by bin/build-runner-image.sh; read at every start, so a rebuilt image
# (and its preloaded store, which prepare snapshots) is picked up by the next job without touching
# this unit.
EnvironmentFile=$IMAGE_ENV
ExecStartPre=+$HELPER prepare $kind %i
ExecStart=${store_lock}/usr/bin/docker run --rm $(container_flags "$(kind_memory "$kind")")--cpuset-cpus \${KNOWN_CI_CPUSET_%i} -v $STATE_DIR/slot-$kind-%i:/home/runner -v $STORE_DIR/slot-$kind-%i:/var/lib/docker -v $RUN_DIR/$kind/%i/jit:/run/jit:ro ${store_mount}--name $NAME-$kind-%i $IMAGE:\${KNOWN_CI_IMAGE_TAG}
ExecStopPost=+$HELPER cleanup $kind %i
# One job per start: the listener starts the next.
Restart=no
# runtime_max_sec, plus warm_max_age_sec for a lane with a warm pool: systemd counts it from the
# unit's start, and the listener recycles a warm slot once it has been up warm_max_age_sec, so a
# job the slot takes just before still has the whole runtime_max_sec.
RuntimeMaxSec=$SLOT_RUNTIME_MAX
# The slot helper's back-off sleeps up to ten minutes inside ExecStartPre.
TimeoutStartSec=15min
# Also the cleanup's budget (ExecStopPost). The cleanup renames the job's store into
# $TRASH_DIR for $REAPER_SERVICE and deletes nothing of it; what can still take minutes is a
# qae store's lock (120 s) and a store the trash could not take, which is deleted here. Deleting one
# (270k files) took 20 to over 60 s on a loaded machine (2026-10-05), and at 60 s systemd killed 195
# of 332 cleanups, whose rm kept running beside the next job's snapshot on the same store.
TimeoutStopSec=300
EOF
}

render_reaper_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's store reaper (bin/lane-slot.sh reap): deletes the store copies its slots retired
# into $TRASH_DIR under one lock: one at a time, or a few at a time while the store is under
# pressure (REAP_JOBS in that script). Every slot's cleanup starts it without waiting (systemctl
# start --no-block), and $REAPER_TIMER does, so no copy is left there by a start that was
# missed or by a reboot. No start timeout (a oneshot has none): emptying a backlog is its job, and a
# killed run would only begin again where it stopped. A run that never ends replaces itself now
# and then, between deletes, with the helper on disk (REAP_MAX_SEC there), so a pulled helper
# reaches it.
[Unit]
Description=$NAME lane: delete the store copies its slots retired
RequiresMountsFor=$STORE_DIR
# Never refuse a start: a cleanup starts it after every job, several times within seconds when jobs
# end together, and a refused start would leave a copy in the trash until the timer.
StartLimitIntervalSec=0

[Service]
Type=oneshot
Environment=KNOWN_CI_NAME=$NAME
Environment=KNOWN_CI_STORE_DIR=$STORE_DIR
ExecStart=$HELPER reap
# The lowest CPU priority, so a delete yields to everything else it shares a core with. It runs
# outside $SLICE on purpose: the lane's CPUQuota and MemoryHigh throttle what is in the slice, and
# the one thing that gives the store its space back must not stall with the jobs that fill it.
Nice=19
EOF
}

render_reaper_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# A minute after boot, then every 5 minutes, counted from the reaper's last activation: the
# backstop for a copy no cleanup's start reached, not the reaper's usual trigger.
[Unit]
Description=$NAME lane: delete retired store copies, every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
}

render_listener_unit() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's scale-set listener: it keeps the lane's runner scale sets, and starts one
# $NAME-<kind>@<n>.service per job GitHub assigns them (listener/README.md).
[Unit]
Description=$NAME lane: scale-set listener (starts its slots on demand)
After=docker.service sysbox.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$LISTENER_BIN $LISTENER_CONFIG
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}

render_token_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Mints the $NAME lane's GitHub App installation token (an hour's life) from the root-only App key
# and writes it to $LANE_USER's gh hosts.yml (0600) (bin/lane-token-refresh.py).
[Unit]
Description=$NAME lane: refresh $LANE_USER's GitHub App installation token
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $TOKEN_REFRESH --app-id $APP_ID --installation-id $APP_INSTALLATION_ID --key-file $KEY_FILE --hosts-out $GH_HOSTS --owner $LANE_USER --gh-user $APP_LOGIN
EOF
}

render_token_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Every 10 minutes, well inside the token's hour.
[Unit]
Description=$NAME lane: refresh $LANE_USER's GitHub App installation token every 10 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF
}

render_listener_health_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's listener health check: restarts $LISTENER_UNIT once, at most every 30 minutes,
# when it is down or its journal is silent (it logs a heartbeat a minute), never for a GitHub
# 401/403, and writes the verdict to $HEALTH_STATE (bin/listener-health.sh).
[Unit]
Description=$NAME lane: check the scale-set listener, restart it once when it is wedged

[Service]
Type=oneshot
ExecStart=$LISTENER_HEALTH $NAME
EOF
}

render_listener_health_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Every 5 minutes, so a listener silent for 3 minutes is restarted within 8.
[Unit]
Description=$NAME lane: check the scale-set listener every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
}

render_image_build_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's weekly image rebuild: --refresh builds $IMAGE:<today>-<hash> unless it exists,
# so each week re-reads the preloaded repositories' supabase/config.toml and re-preloads the images
# they start (bin/build-runner-image.sh). --if-provisioned makes it a logged no-op until Sysbox and
# the lane's store exist. It holds the lane's build lock, as every caller of the build does.
[Unit]
Description=$NAME lane: rebuild the runner image $IMAGE
Wants=network-online.target
After=docker.service network-online.target

[Service]
Type=oneshot
WorkingDirectory=$CHECKOUT
ExecStart=/usr/bin/flock -o $BUILD_LOCK $IMAGE_BUILD $HOST $NAME --refresh --if-provisioned
EOF
}

render_image_build_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Weekly, up to an hour late at random; Persistent= runs a week the machine was down through at its boot.
[Unit]
Description=$NAME lane: rebuild the runner image $IMAGE weekly

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
EOF
}

render_codex_config() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's tracked Codex config: bin/lane-slot.sh writes it into the Codex store before
# every QAE job (the QAE harness's Codex action requires a file-backed login store).
cli_auth_credentials_store = "file"
EOF
}

render_keepalive_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# The $NAME lane's Codex login keepalive (bin/lane-slot.sh codex-keepalive): each qae instance's
# store that no job has refreshed for 10 days gets one trivial codex exec as $LANE_USER, which makes
# Codex refresh the login, and is pruned back to auth.json. A store a job holds is skipped. Exits
# non-zero when a store it ran Codex on was not refreshed.
[Unit]
Description=$NAME lane: keep each QAE instance's Codex login fresh
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=KNOWN_CI_NAME=$NAME
Environment=KNOWN_CI_STATE_DIR=$STATE_DIR
Environment=KNOWN_CI_RUN_DIR=$RUN_DIR
$(render_codex_stores)
Environment=KNOWN_CI_CODEX_CONFIG=$CODEX_CONFIG
Environment=KNOWN_CI_CODEX_OWNER=$LANE_USER
ExecStart=$HELPER codex-keepalive
# Each store's Codex run is bounded at 100 s; this bounds the whole pass.
TimeoutStartSec=30min
EOF
}

render_keepalive_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-lane.sh); do not edit on the machine.
# Daily, up to an hour late at random; Persistent= runs a day the machine was down through at its boot.
[Unit]
Description=$NAME lane: keep each QAE instance's Codex login fresh, daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
EOF
}

render_listener_config() {
  local args=(listener-config "$HOST_YML" "$NAME" --key-file "$KEY_FILE")
  ! has_qae || args+=(--codex-store "$CODEX_STORE_DIR")
  python3 "$REPO_ROOT/lib/lanes.py" "${args[@]}"
}
