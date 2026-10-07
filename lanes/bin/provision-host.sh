#!/usr/bin/env bash
# provision-host.sh: converge what every lane on a machine shares, once per machine.
#
#   provision-host.sh <host> [--check | --apply | --remove [--apply]]
#
# <host> names hosts/<host>.yml in the machine's values checkout. The machine's lanes
# (bin/provision-lane.sh) run every job in its
# own Sysbox system container; this script gives them the host they need: Docker CE, Sysbox CE on
# its own bounded filesystem, the apt holds that keep both where Sysbox supports them, and the
# self-update that keeps the machine's two checkouts, the engine and the values, on their
# origin/main (bin/runners-pull.sh). Design: README.md.
#
# Operator-invoked and dry-run by default: --apply mutates, --check is a read-only report that exits
# non-zero until the host is converged, --remove plans the teardown (and runs it with --apply).
# runners-pull.service runs --apply after every change to main.
#
# Linux-only by design: Ubuntu 24.04, bash 5, GNU coreutils, util-linux, apt, systemd, rootful
# Docker. Run it as root from the machine's engine checkout (/opt/runner-lanes/lanes): the
# self-update unit runs bin/runners-pull.sh from there.
#
# --apply converges, in order, each step skipped when already converged:
#   A   jq, python3-yaml, python3-jwt and python3-cryptography (the scripts read JSON and the lane
#       config, and the token refresh signs its App JWT with RS256, which PyJWT does through
#       cryptography; python3-jwt only recommends it)
#   A2  Docker CE, only where it is absent, from Docker's apt repository at the versions the runner
#       image's Dockerfile pins (the host and the inner engine run the same release)
#   B   /etc/docker/daemon.json carries bip and default-address-pools, set to the live docker0
#       address and Docker 29's built-in pools, so the Sysbox installer does not restart Docker
#       (its documented no-restart path) and a later restart changes nothing
#   C   Sysbox's per-container data directory /var/lib/sysbox on its own bounded, sparse ext4 image
#       (hosts.sysbox.image), mounted by var-lib-sysbox.mount before any Sysbox service starts;
#       an existing image at that path is adopted as it is
#   D   sysbox-ce 0.7.1 from its release .deb, sha256-pinned; Docker must not restart
#   E   apt-mark hold on sysbox-ce, docker-ce, docker-ce-cli, containerd.io and the installed kernel
#       meta packages, so unattended upgrades cannot outrun what Sysbox supports
#   F   runners-pull.service and runners-pull.timer (every 5 minutes, root): the machine's
#       self-update, the engine from its public origin/main and the values checkout from its own
#       through the read-only deploy key /etc/runners/deploy_key
# Steps end at OPERATOR ACTION gates this script never performs: the deploy key, and the machine's
# /etc/runners-host naming this host config.
#
# --remove disables and deletes the self-update units and its record. It leaves Docker, Sysbox, its
# filesystem, the daemon.json keys, the apt holds, the deploy key and every lane in place.
# shellcheck disable=SC2153 # the settings are assigned by read_settings (lib/provision.sh)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=lib/provision.sh
. "$REPO_ROOT/lib/provision.sh"

# Every path is overridable only so the hermetic test can run unprivileged.
UNIT_DIR="${RUNNERS_UNIT_DIR:-/etc/systemd/system}"
SYSBOX_DIR="${RUNNERS_SYSBOX_DIR:-/var/lib/sysbox}"
DAEMON_JSON="${RUNNERS_DAEMON_JSON:-/etc/docker/daemon.json}"
APT_DIR="${RUNNERS_APT_DIR:-/etc/apt}"
# The machine's two checkouts: the engine (a clone of the catalog, whose lanes/ directory is this
# kit) and the values (the owner's private repository, whose root holds hosts/<host>.yml).
ENGINE="${RUNNERS_ENGINE:-/opt/runner-lanes}"
CONFIG="${RUNNERS_CONFIG:-/opt/runner-lanes-config}"
# The units run the kit in the machine's engine checkout, never the one this script happens to run from.
CHECKOUT="$ENGINE/lanes"
RUNNERS_ETC="${RUNNERS_ETC_DIR:-/etc/runners}"
DEPLOY_KEY="$RUNNERS_ETC/deploy_key"
HOST_FILE="${RUNNERS_HOST_FILE:-/etc/runners-host}"
PULL_STATE="${RUNNERS_PULL_STATE_DIR:-/var/lib/runners}"
PULL_SERVICE="runners-pull.service"
PULL_TIMER="runners-pull.timer"
# The runner image pins Docker CE for its inner engine; the host installs the same release.
DOCKERFILE="$REPO_ROOT/image/Dockerfile"
SYSBOX_MOUNT_UNIT="var-lib-sysbox.mount"
SYSBOX_DROPIN_DIR="$UNIT_DIR/sysbox-mgr.service.d"
# A legacy name, kept for machines already running: they carry this file.
SYSBOX_DROPIN="$SYSBOX_DROPIN_DIR/known-ci-storage.conf"
# The release .deb of the Sysbox pin (lib/provision.sh). The checksum is the one the release notes
# publish and GitHub's asset digest reports; nothing is installed from a file that does not match it.
SYSBOX_DEB="sysbox-ce_${SYSBOX_VERSION}.linux_amd64.deb"
SYSBOX_URL="https://github.com/nestybox/sysbox/releases/download/v${SYSBOX_VERSION}/${SYSBOX_DEB}"
SYSBOX_SHA256="9d6d5484f980d0a17f86c492c1262015c2afb66280bdb97215b79fde6a0261c5"
HOST_PACKAGES=(jq python3-yaml python3-jwt python3-cryptography)
HELD_PACKAGES=(sysbox-ce docker-ce docker-ce-cli containerd.io)
KERNEL_META_CANDIDATES=(
  linux-generic linux-image-generic linux-headers-generic
  linux-virtual linux-image-virtual linux-headers-virtual
  linux-generic-hwe-24.04 linux-image-generic-hwe-24.04 linux-headers-generic-hwe-24.04
  linux-virtual-hwe-24.04 linux-image-virtual-hwe-24.04 linux-headers-virtual-hwe-24.04
)
# Docker 29's built-in local address pools (moby daemon/libnetwork/ipamutils), written verbatim so
# declaring them changes no allocation.
DOCKER_DEFAULT_POOLS='[{"base":"172.17.0.0/16","size":16},{"base":"172.18.0.0/16","size":16},{"base":"172.19.0.0/16","size":16},{"base":"172.20.0.0/14","size":16},{"base":"172.24.0.0/14","size":16},{"base":"172.28.0.0/14","size":16},{"base":"192.168.0.0/16","size":20}]'
# The apt source the image's Dockerfile writes; the host uses the same one (a test holds them equal).
DOCKER_APT_SOURCE='deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable'
DOCKER_GPG_URL="https://download.docker.com/linux/ubuntu/gpg"
APPLY=0
CHECK=0
REMOVE=0
HOST=""

usage() {
  cat <<EOF
usage: $0 <host> [--check | --apply | --remove [--apply]]

<host> names hosts/<host>.yml in the values checkout. Default is dry-run planning. --apply converges
the host. --check is read-only and exits non-zero until the host is converged. --remove plans the
teardown; --remove --apply performs it.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --check) CHECK=1 ;;
    --remove) REMOVE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown arg: $1" ;;
    *) [ -z "$HOST" ] || die "one host only, got '$HOST' and '$1'"; HOST="$1" ;;
  esac
  shift
done
[ -n "$HOST" ] || { usage >&2; exit 2; }
[[ "$HOST" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid host '$HOST' (allowed: a-z 0-9 -)"
[ "$CHECK" = "1" ] && { [ "$APPLY" = "1" ] || [ "$REMOVE" = "1" ]; } && die "--check is read-only; do not combine it with --apply or --remove"

if [ "${RUNNERS_ALLOW_NON_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" = "0" ] || die "must run as root (packages, mounts, systemd units, Docker)"
fi
HOST_YML="${RUNNERS_HOST_YML:-$CONFIG/hosts/$HOST.yml}"
[ -r "$HOST_YML" ] || die "no host config at $HOST_YML"

# ---- renderers -------------------------------------------------------------------------------------
render_sysbox_mount() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-host.sh); do not edit on the machine.
# Sysbox's per-container data lives under /var/lib/sysbox. This bounds it to its own filesystem so
# a burst of containers cannot fill the root filesystem.
[Unit]
Description=Bounded filesystem for Sysbox's per-container data
RequiresMountsFor=$(dirname "$SYSBOX_IMAGE")
Before=sysbox-mgr.service

[Mount]
What=$SYSBOX_IMAGE
Where=$SYSBOX_DIR
Type=ext4
# discard: a container's data is freed at its exit; without discard the sparse image only grows
# (32G of dead blocks, 2026-09-26).
Options=loop,discard

[Install]
WantedBy=multi-user.target
EOF
}

render_sysbox_dropin() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-host.sh); do not edit on the machine.
# sysbox-mgr must never start on the root filesystem's /var/lib/sysbox.
[Unit]
RequiresMountsFor=$SYSBOX_DIR
EOF
}

render_pull_service() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-host.sh); do not edit on the machine.
# Fast-forwards $ENGINE and $CONFIG to their origin/main and, when either moved or the last apply
# failed, converges this machine: provision-host.sh --apply, provision-lane.sh --apply for every lane
# of hosts/<host>.yml, then provision-dashboards.sh --apply, <host> being $HOST_FILE
# (bin/runners-pull.sh).
[Unit]
Description=runners: update $ENGINE and $CONFIG from origin/main and converge this machine
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=$CHECKOUT/bin/runners-pull.sh
TimeoutStartSec=30min
EOF
}

render_pull_timer() {
  cat <<EOF
# Managed by the runner lanes kit (bin/provision-host.sh); do not edit on the machine.
[Unit]
Description=runners: update and converge this machine every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
}

# ---- reads ------------------------------------------------------------------------------------------
kernel_metas() {
  dpkg-query -W -f='${db:Status-Abbrev}|${Package}\n' "${KERNEL_META_CANDIDATES[@]}" 2>/dev/null \
    | awk -F'|' '$1 ~ /^ii/ { print $2 }'
}

live_bip() {
  local out gateway subnet
  out="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}} {{(index .IPAM.Config 0).Subnet}}' 2>/dev/null)" || return 1
  read -r gateway subnet <<< "$out"
  [ -n "${gateway:-}" ] && [ -n "${subnet:-}" ] || return 1
  printf '%s/%s\n' "$gateway" "${subnet#*/}"
}

dockerfile_arg() { sed -n "s/^ARG $1=//p" "$DOCKERFILE" | tail -n 1; }

# ---- apply phases -----------------------------------------------------------------------------------
ensure_packages() {
  log "Phase A: ${HOST_PACKAGES[*]}"
  local pkg missing=()
  for pkg in "${HOST_PACKAGES[@]}"; do
    [ -n "$(installed_version "$pkg")" ] || missing+=("$pkg")
  done
  if [ "${#missing[@]}" -eq 0 ]; then log "  present"; return 0; fi
  run_mutation env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

ensure_docker() {
  log "Phase A2: Docker CE (installed only where it is absent)"
  local version containerd gpg_sha tmp actual
  version="$(installed_version docker-ce)"
  if [ -n "$version" ]; then log "  present ($version)"; return 0; fi
  version="$(dockerfile_arg DOCKER_VERSION)"
  containerd="$(dockerfile_arg CONTAINERD_VERSION)"
  gpg_sha="$(dockerfile_arg DOCKER_GPG_SHA256)"
  [ -n "$version" ] && [ -n "$containerd" ] && [ -n "$gpg_sha" ] \
    || die "cannot read DOCKER_VERSION, CONTAINERD_VERSION and DOCKER_GPG_SHA256 from $DOCKERFILE"
  if [ "$APPLY" != "1" ]; then
    log "DRY-RUN: install docker-ce=$version docker-ce-cli=$version containerd.io=$containerd from Docker's apt repository (its key by sha256 $gpg_sha), the runner image's own pins"
    return 0
  fi
  tmp="$(mktemp -d)"
  log "RUN: curl $DOCKER_GPG_URL"
  curl -fsSL --retry 3 -o "$tmp/docker.asc" "$DOCKER_GPG_URL" || { rm -rf "$tmp"; die "download failed: $DOCKER_GPG_URL"; }
  actual="$(hash_file "$tmp/docker.asc")"
  if [ "$actual" != "$gpg_sha" ]; then
    rm -rf "$tmp"
    die "Docker's apt key sha256 $actual does not match the image's pin $gpg_sha; nothing installed"
  fi
  install -d -m 0755 "$APT_DIR/keyrings" "$APT_DIR/sources.list.d"
  install -m 0644 "$tmp/docker.asc" "$APT_DIR/keyrings/docker.asc"
  rm -rf "$tmp"
  printf '%s\n' "$DOCKER_APT_SOURCE" > "$APT_DIR/sources.list.d/docker.list"
  run_mutation apt-get update
  run_mutation env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    "docker-ce=$version" "docker-ce-cli=$version" "containerd.io=$containerd"
  [ -n "$(installed_version docker-ce)" ] || die "docker-ce is not installed after apt-get install"
  log "  installed docker-ce $version and containerd.io $containerd"
}

ensure_daemon_json() {
  log "Phase B: $DAEMON_JSON carries bip and default-address-pools (no Docker restart)"
  local bip current current_bip new tmp
  if ! bip="$(live_bip)"; then
    # A dry run on a host Phase A2 has not given Docker yet has no docker0 to read.
    [ "$APPLY" = 1 ] || { log "  docker0 not readable yet (Docker absent); planned after Phase A2"; return 0; }
    die "cannot read docker0's address from 'docker network inspect bridge'"
  fi
  if [ -f "$DAEMON_JSON" ]; then
    current="$(cat "$DAEMON_JSON")"
    jq -e 'type == "object"' <<< "$current" >/dev/null 2>&1 || die "$DAEMON_JSON is not a JSON object; refusing to rewrite it"
  else
    current='{}'
  fi
  current_bip="$(jq -r '.bip // empty' <<< "$current")"
  if [ -n "$current_bip" ] && [ "$current_bip" != "$bip" ]; then
    die "$DAEMON_JSON sets bip=$current_bip but docker0 runs $bip; a Docker restart would move the bridge. Resolve by hand first."
  fi
  if [ "$current_bip" = "$bip" ] && jq -e 'has("default-address-pools")' <<< "$current" >/dev/null; then
    log "  converged (bip=$bip, default-address-pools present)"
    return 0
  fi
  new="$(jq --arg bip "$bip" --argjson pools "$DOCKER_DEFAULT_POOLS" \
    '. + {bip: $bip} + (if has("default-address-pools") then {} else {"default-address-pools": $pools} end)' <<< "$current")"
  if [ "$APPLY" != "1" ]; then
    log "DRY-RUN: write $DAEMON_JSON with bip=$bip and Docker's default address pools (dockerd --validate first)"
    return 0
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$new" > "$tmp"
  log "RUN: dockerd --validate --config-file $tmp"
  dockerd --validate --config-file "$tmp" >/dev/null || { rm -f "$tmp"; die "dockerd rejected the merged daemon.json; nothing written"; }
  if [ -f "$DAEMON_JSON" ]; then
    cp -p "$DAEMON_JSON" "$DAEMON_JSON.pre-runners.$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  install -d -m 0755 "$(dirname "$DAEMON_JSON")"
  install -m 0644 "$tmp" "$DAEMON_JSON"
  rm -f "$tmp"
  log "  wrote $DAEMON_JSON (bip=$bip); Docker was not restarted and does not need to be"
}

ensure_sysbox_storage() {
  log "Phase C: $SYSBOX_DIR on its own ${SYSBOX_GB}G filesystem ($SYSBOX_IMAGE)"
  local changed=""
  if [ -n "$(installed_version sysbox-ce)" ] && ! mountpoint -q "$SYSBOX_DIR" 2>/dev/null; then
    die "sysbox-ce is installed but $SYSBOX_DIR is not a mount; mounting over it would hide live Sysbox data. Stop every Sysbox container and sysbox, move the directory aside, then re-run."
  fi
  ensure_dir "$(dirname "$SYSBOX_IMAGE")" 0700
  if [ -e "$SYSBOX_IMAGE" ]; then
    log "  $SYSBOX_IMAGE present"
  else
    run_mutation truncate -s "${SYSBOX_GB}G" "$SYSBOX_IMAGE"
    run_mutation mkfs.ext4 -q -F -m 0 "$SYSBOX_IMAGE"
  fi
  run_mutation install -d -m 0755 "$SYSBOX_DIR"
  changed+="$(converge_file "$UNIT_DIR/$SYSBOX_MOUNT_UNIT" "$(render_sysbox_mount)")"
  changed+="$(converge_file "$SYSBOX_DROPIN" "$(render_sysbox_dropin)")"
  [ -n "$changed" ] && run_mutation systemctl daemon-reload
  if mountpoint -q "$SYSBOX_DIR" 2>/dev/null; then
    log "  $SYSBOX_DIR mounted"
    # A changed unit does not touch the live mount, and restarting a .mount unit would unmount
    # under sysbox-mgr; a remount applies the options in place.
    [ -z "$changed" ] || run_mutation mount -o remount,discard "$SYSBOX_DIR"
  else
    run_mutation systemctl enable --now "$SYSBOX_MOUNT_UNIT"
  fi
}

ensure_sysbox() {
  log "Phase D: sysbox-ce $SYSBOX_VERSION"
  local version tmp actual before after
  version="$(installed_version sysbox-ce)"
  if sysbox_version_pinned "$version"; then log "  installed ($version)"; return 0; fi
  [ -z "$version" ] || die "sysbox-ce $version is installed; this kit pins $SYSBOX_VERSION. Sysbox upgrades are uninstall-then-install with every Sysbox container stopped (README.md)."
  if [ "$APPLY" != "1" ]; then
    log "DRY-RUN: download $SYSBOX_URL, verify sha256 $SYSBOX_SHA256, apt-get install it without restarting Docker"
    return 0
  fi
  mountpoint -q "$SYSBOX_DIR" || die "$SYSBOX_DIR is not mounted; Phase C must converge first"
  jq -e 'has("bip") and has("default-address-pools")' "$DAEMON_JSON" >/dev/null 2>&1 \
    || die "$DAEMON_JSON lacks bip/default-address-pools; the Sysbox installer would restart Docker"
  tmp="$(mktemp -d)"
  log "RUN: curl $SYSBOX_URL"
  curl -fsSL --retry 3 -o "$tmp/$SYSBOX_DEB" "$SYSBOX_URL" || { rm -rf "$tmp"; die "download failed: $SYSBOX_URL"; }
  actual="$(hash_file "$tmp/$SYSBOX_DEB")"
  if [ "$actual" != "$SYSBOX_SHA256" ]; then
    rm -rf "$tmp"
    die "sysbox package sha256 $actual does not match the pinned $SYSBOX_SHA256; nothing installed"
  fi
  before="$(systemctl show docker -p ExecMainStartTimestampMonotonic --value 2>/dev/null)"
  log "RUN: apt-get install -y $tmp/$SYSBOX_DEB"
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$tmp/$SYSBOX_DEB" || { rm -rf "$tmp"; die "apt-get could not install $SYSBOX_DEB"; }
  rm -rf "$tmp"
  after="$(systemctl show docker -p ExecMainStartTimestampMonotonic --value 2>/dev/null)"
  [ "$before" = "$after" ] || die "Docker restarted during the Sysbox install (its containers restarted with it); investigate before going on"
  docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"sysbox-runc"' \
    || die "Docker does not list the sysbox-runc runtime after the install"
  log "  installed; Docker lists sysbox-runc and was not restarted"
}

ensure_holds() {
  log "Phase E: apt holds"
  local held want=() pkg to_hold=() metas=()
  held="$(apt-mark showhold 2>/dev/null || true)"
  mapfile -t metas < <(kernel_metas)
  for pkg in "${HELD_PACKAGES[@]}" "${metas[@]}"; do
    [ -n "$(installed_version "$pkg")" ] || [ "$APPLY" != "1" ] || continue
    want+=("$pkg")
    grep -qx "$pkg" <<< "$held" || to_hold+=("$pkg")
  done
  if [ "${#to_hold[@]}" -eq 0 ]; then
    log "  held: ${want[*]}"
  else
    run_mutation apt-mark hold "${to_hold[@]}"
  fi
}

ensure_self_update() {
  log "Phase F: $PULL_TIMER updates $ENGINE and $CONFIG from origin/main every 5 minutes"
  local changed=""
  ensure_dir "$RUNNERS_ETC" 0700
  changed+="$(converge_file "$UNIT_DIR/$PULL_SERVICE" "$(render_pull_service)")"
  changed+="$(converge_file "$UNIT_DIR/$PULL_TIMER" "$(render_pull_timer)")"
  [ -z "$changed" ] || run_mutation systemctl daemon-reload
  run_mutation systemctl enable --now "$PULL_TIMER"
}

print_gates() {
  local named origin values
  if [ ! -s "$DEPLOY_KEY" ]; then
    # The values repository is whatever this machine's values checkout was cloned from.
    origin="$(git -C "$CONFIG" remote get-url origin 2>/dev/null || true)"
    if [ -n "$origin" ]; then
      values="$origin, cloned at $CONFIG"
    else
      values="the private repository whose root holds hosts/$HOST.yml; no checkout at $CONFIG names it yet, so clone it there once the key is added"
    fi
    gate "Give this machine read-only access to its values repository ($values): run ssh-keygen -t ed25519 -N '' -C runners-$HOST -f $DEPLOY_KEY on the machine, then add $DEPLOY_KEY.pub to that repository as a deploy key from a login with admin on it: ssh root@$HOST_NAME cat $DEPLOY_KEY.pub > runners-$HOST.pub && gh repo deploy-key add runners-$HOST.pub --repo <owner>/<repository> --title runners-$HOST (read-only unless --allow-write is given). Until then $PULL_SERVICE cannot fetch $CONFIG."
  fi
  named="$(tr -d '[:space:]' < "$HOST_FILE" 2>/dev/null || true)"
  if [ "$named" != "$HOST" ]; then
    gate "Name this machine's host config: echo $HOST > $HOST_FILE (it reads '${named:-nothing}'). $PULL_SERVICE converges the lanes of hosts/<that name>.yml in $CONFIG."
  fi
}

# ---- remove -----------------------------------------------------------------------------------------
remove_host() {
  log "Remove: the self-update ($PULL_TIMER, $PULL_SERVICE and its record)"
  if [ -f "$UNIT_DIR/$PULL_TIMER" ]; then
    run_mutation systemctl disable --now "$PULL_TIMER"
  fi
  run_mutation rm -f "$UNIT_DIR/$PULL_SERVICE" "$UNIT_DIR/$PULL_TIMER"
  run_mutation systemctl daemon-reload
  run_mutation rm -rf "$PULL_STATE"
  log "Left in place: Docker, sysbox-ce and $SYSBOX_MOUNT_UNIT ($SYSBOX_IMAGE), the daemon.json keys, the apt holds, the deploy key $DEPLOY_KEY, $HOST_FILE and every lane (remove each first: bin/provision-lane.sh $HOST <lane> --remove --apply)."
}

# ---- check ------------------------------------------------------------------------------------------
fail=0
bad() { fail=1; }

check_packages() {
  local pkg v held metas=() unheld="" absent=""
  for pkg in "${HOST_PACKAGES[@]}"; do [ -n "$(installed_version "$pkg")" ] || absent+="$pkg "; done
  printf 'host_packages=%s\n' "${absent:-present}"
  [ -z "$absent" ] || bad
  printf 'packages='
  for pkg in "${HELD_PACKAGES[@]}"; do
    v="$(installed_version "$pkg")"
    printf '%s:%s ' "$pkg" "${v:-absent}"
  done
  printf 'kernel=%s\n' "$(uname -r)"
  sysbox_version_pinned "$(installed_version sysbox-ce)" || bad
  held="$(apt-mark showhold 2>/dev/null || true)"
  mapfile -t metas < <(kernel_metas)
  for pkg in "${HELD_PACKAGES[@]}" "${metas[@]}"; do
    grep -qx "$pkg" <<< "$held" || unheld+="$pkg "
  done
  printf 'holds=%s kernel_meta=%s missing_holds=%s\n' "$(tr '\n' ' ' <<< "$held" | sed 's/ $//')" "${metas[*]}" "${unheld:-none}"
  [ -z "$unheld" ] || bad
}

check_docker() {
  local runtimes storage bip pools services s state
  runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
  storage="$(docker info --format '{{.Driver}}' 2>/dev/null || true)"
  bip="$(jq -r '.bip // "absent"' "$DAEMON_JSON" 2>/dev/null || echo absent)"
  pools="$(jq -r 'if has("default-address-pools") then "present" else "absent" end' "$DAEMON_JSON" 2>/dev/null || echo absent)"
  if grep -q '"sysbox-runc"' <<< "$runtimes"; then printf 'docker_runtime_sysbox=yes '; else printf 'docker_runtime_sysbox=no '; bad; fi
  printf 'docker_storage=%s daemon_json_bip=%s daemon_json_pools=%s\n' "${storage:-unknown}" "$bip" "$pools"
  { [ "$bip" != absent ] && [ "$pools" = present ]; } || bad
  services=""
  for s in sysbox sysbox-mgr sysbox-fs; do
    state="$(systemctl is-active "$s" 2>/dev/null || true)"
    services+="$s:${state:-unknown} "
    [ "$state" = active ] || bad
  done
  printf 'sysbox_services=%s\n' "${services% }"
}

check_storage() {
  local src free dir
  if mountpoint -q "$SYSBOX_DIR" 2>/dev/null; then
    src="$(findmnt -n -o SOURCE,FSTYPE,SIZE "$SYSBOX_DIR" 2>/dev/null | tr -s ' ')"
    printf 'sysbox_fs=mounted (%s) mount_unit=%s\n' "${src:-?}" "$(systemctl is-enabled "$SYSBOX_MOUNT_UNIT" 2>/dev/null || echo unknown)"
  else
    printf 'sysbox_fs=not-mounted\n'
    bad
  fi
  if [ "$(cat "$UNIT_DIR/$SYSBOX_MOUNT_UNIT" 2>/dev/null)" != "$(render_sysbox_mount)" ]; then
    printf 'sysbox_mount_unit=%s absent or DRIFTED from this checkout (re-run --apply)\n' "$SYSBOX_MOUNT_UNIT"; bad
  fi
  dir="$(dirname "$SYSBOX_IMAGE")"
  [ -d "$dir" ] || dir="$(dirname "$dir")"
  free="$(df -BG --output=avail "$dir" 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  printf 'disk free=%sG sysbox_image=%s (%sG, sparse)\n' "${free:-?}" "$SYSBOX_IMAGE" "$SYSBOX_GB"
  if [ -n "$free" ] && [ "$free" -lt "$SYSBOX_GB" ]; then
    warn "the Sysbox filesystem could grow past the free space on $dir (${free}G < ${SYSBOX_GB}G)"
  fi
}

check_self_update() {
  local state named applied heads
  state="$(systemctl is-active "$PULL_TIMER" 2>/dev/null || true)"
  printf 'self_update_timer=%s %s\n' "$PULL_TIMER" "${state:-unknown}"
  [ "$state" = active ] || bad
  if [ "$(cat "$UNIT_DIR/$PULL_SERVICE" 2>/dev/null)" != "$(render_pull_service)" ] || [ "$(cat "$UNIT_DIR/$PULL_TIMER" 2>/dev/null)" != "$(render_pull_timer)" ]; then
    printf 'self_update_units=absent or DRIFTED from this checkout (re-run --apply)\n'; bad
  fi
  if [ -s "$DEPLOY_KEY" ]; then printf 'deploy_key=%s present\n' "$DEPLOY_KEY"; else printf 'deploy_key=%s absent\n' "$DEPLOY_KEY"; bad; fi
  named="$(tr -d '[:space:]' < "$HOST_FILE" 2>/dev/null || true)"
  printf 'runners_host=%s\n' "${named:-absent}"
  [ "$named" = "$HOST" ] || bad
  # bin/runners-pull.sh records the two HEADs it applied, in this form.
  applied="$(cat "$PULL_STATE/applied" 2>/dev/null || true)"
  heads="engine=$(git -C "$ENGINE" rev-parse HEAD 2>/dev/null || echo unknown) config=$(git -C "$CONFIG" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ "$applied" = "$heads" ]; then
    printf 'last_applied=%s (the HEADs of both checkouts)\n' "$applied"
  else
    printf 'last_applied=%s checkout_heads=%s (an apply is pending or failing: journalctl -u %s)\n' "${applied:-none}" "$heads" "$PULL_SERVICE"
  fi
}

check_report() {
  log "Check mode: host=$HOST ($HOST_NAME) lanes=[$LANES] sysbox=$SYSBOX_IMAGE"
  check_packages
  check_docker
  check_storage
  check_self_update
  [ "$fail" = 0 ]
}

take_apply_lock
[ "$CHECK" = 1 ] || [ "$REMOVE" = 1 ] || ensure_packages
read_settings host-env "$HOST_YML"
log "host=$HOST ($HOST_NAME) lanes=[$LANES] sysbox=$SYSBOX_IMAGE (${SYSBOX_GB}G) apply=$APPLY check=$CHECK remove=$REMOVE"
if [ "$CHECK" = "1" ]; then
  check_report || exit 1
  exit 0
fi
if [ "$REMOVE" = "1" ]; then
  remove_host
  log "done"
  exit 0
fi
ensure_docker
ensure_daemon_json
ensure_sysbox_storage
ensure_sysbox
ensure_holds
ensure_self_update
print_gates
log "done"
