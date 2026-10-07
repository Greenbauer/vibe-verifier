#!/bin/bash
# provision-host-test.sh: hermetic tests for bin/provision-host.sh. The system tools are stubs on
# PATH (tests/lib/stubs.sh); the script runs from a copy of the kit whose Sysbox checksum and
# Docker key pin match the test's fixtures, and reads its host file from a stand-in values checkout.
# No real package, mount, unit or network change happens.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/stubs.sh
. "$ROOT/tests/lib/stubs.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }
sha() { sha256sum "$1" | awk '{print $1}'; }
gitc() { git -c user.name=t -c user.email=t@example.invalid "$@"; }
mode_of() { python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$1" 2>/dev/null; }
POOLS='[{"base":"172.17.0.0/16","size":16},{"base":"172.18.0.0/16","size":16},{"base":"172.19.0.0/16","size":16},{"base":"172.20.0.0/14","size":16},{"base":"172.24.0.0/14","size":16},{"base":"172.28.0.0/14","size":16},{"base":"192.168.0.0/16","size":20}]'

setup() {
  S="$TMP/s"
  rm -rf "$S"
  mkdir -p "$S/repo/bin" "$S/repo/lib" "$S/repo/image" "$S/config/hosts" "$S/units" "$S/sysbox" "$S/etc-docker" "$S/st/state" "$S/fx" "$S/imgdir"
  cp "$ROOT"/lib/*.py "$ROOT/lib/provision.sh" "$S/repo/lib/"
  printf 'fake sysbox package\n' > "$S/fx/sysbox.deb"
  printf 'fake docker apt key\n' > "$S/fx/docker.asc"
  sed "s/^SYSBOX_SHA256=.*/SYSBOX_SHA256=\"$(sha "$S/fx/sysbox.deb")\"/" "$ROOT/bin/provision-host.sh" > "$S/repo/bin/provision-host.sh"
  # The runner image's Dockerfile, with the Docker apt key pinned to the fixture's.
  sed "s/^ARG DOCKER_GPG_SHA256=.*/ARG DOCKER_GPG_SHA256=$(sha "$S/fx/docker.asc")/" "$ROOT/image/Dockerfile" > "$S/repo/image/Dockerfile"
  # The host config: the Sysbox image path is the config's, adopted wherever it lives.
  sed "s#^sysbox: .*#sysbox: {image: $S/imgdir/sysbox.img, disk_gb: 20}#" "$ROOT/examples/hosts/example.yml" > "$S/config/hosts/box.yml"
  # The machine's two checkouts, as runners-pull leaves them: the engine, a clone with a HEAD, and
  # the values checkout, a clone of the owner's private repository holding the host file.
  git init -q "$S/engine" && gitc -C "$S/engine" commit -q --allow-empty -m one
  git init -q "$S/config" && gitc -C "$S/config" add -A && gitc -C "$S/config" commit -q -m one
  git -C "$S/config" remote add origin git@git.example.invalid:someone/lane-values.git
  printf 'docker-ce\n' > "$S/st/holds"
  : > "$S/st/mounts"
  CALLLOG="$S/calls.log"; : > "$CALLLOG"
  export CALLLOG ST="$S/st" FX="$S/fx" UNITS="$S/units"
  write_stubs "$S/pathbin"
}

# The host file is the values checkout's hosts/<host>.yml unless a test names another file.
run_host() {
  env PATH="$S/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 \
    ${HOST_YML_UNDER_TEST:+RUNNERS_HOST_YML="$HOST_YML_UNDER_TEST"} \
    RUNNERS_UNIT_DIR="$S/units" RUNNERS_SYSBOX_DIR="$S/sysbox" RUNNERS_DAEMON_JSON="$S/etc-docker/daemon.json" \
    RUNNERS_APT_DIR="$S/apt" RUNNERS_ENGINE="$S/engine" RUNNERS_CONFIG="${RUNNERS_CONFIG_UNDER_TEST:-$S/config}" RUNNERS_ETC_DIR="$S/etc-runners" \
    RUNNERS_HOST_FILE="$S/runners-host" RUNNERS_PULL_STATE_DIR="$S/pull-state" RUNNERS_APPLY_LOCK="$S/apply.lock" \
    bash "$S/repo/bin/provision-host.sh" "${HOST_UNDER_TEST:-box}" "$@"
}

# A converged host: what a successful --apply leaves, plus the operator's deploy key and host name.
converged() {
  run_host --apply >/dev/null 2>&1
  mkdir -p "$S/etc-runners"; printf 'key\n' > "$S/etc-runners/deploy_key"; printf 'box\n' > "$S/runners-host"
  # The record bin/runners-pull.sh writes after a successful apply.
  mkdir -p "$S/pull-state"; printf 'engine=%s config=%s\n' "$(git -C "$S/engine" rev-parse HEAD)" "$(git -C "$S/config" rev-parse HEAD)" > "$S/pull-state/applied"
  : > "$CALLLOG"
}

MUTATIONS='^(docker network (create|rm)|dockerd|apt-get|apt-mark hold|systemctl (enable|disable|daemon-reload|start|stop|restart)|mkfs|truncate|mount |umount|curl|useradd|chown|chgrp)'
line_of() { grep -nE -- "$1" "$CALLLOG" | head -n 1 | cut -d: -f1; }
before() { local a b; a="$(line_of "$1")"; b="$(line_of "$2")"; [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; }
IMG="$TMP/s/imgdir/sysbox.img"

# ---- dry-run (the default) ---------------------------------------------------------------------------
setup
out="$(run_host 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "default dry-run exits 0"
grep -q "Phase A: jq python3-yaml python3-jwt python3-cryptography" <<< "$out" && grep -A1 "Phase A:" <<< "$out" | grep -q "present"; expect $? "the host packages are present, so Phase A plans nothing"
grep -q "DRY-RUN: write $S/etc-docker/daemon.json with bip=172.17.0.1/16" <<< "$out"; expect $? "dry-run plans daemon.json with the live docker0 address"
grep -q "DRY-RUN: truncate -s 20G $IMG" <<< "$out" && grep -q "DRY-RUN: mkfs.ext4 -q -F -m 0 $IMG" <<< "$out" && grep -q "DRY-RUN: systemctl enable --now var-lib-sysbox.mount" <<< "$out"; expect $? "dry-run plans the bounded Sysbox filesystem at hosts.sysbox.image"
grep -q "DRY-RUN: download https://github.com/nestybox/sysbox/releases/download/v0.7.1/sysbox-ce_0.7.1.linux_amd64.deb, verify sha256" <<< "$out"; expect $? "dry-run plans the pinned, checksum-verified Sysbox release"
grep -q "DRY-RUN: apt-mark hold sysbox-ce docker-ce-cli containerd.io linux-image-virtual" <<< "$out"; expect $? "dry-run plans holds on the unheld packages and the installed kernel meta only"
grep -q "DRY-RUN: write $S/units/runners-pull.service" <<< "$out" && grep -q "DRY-RUN: write $S/units/runners-pull.timer" <<< "$out" && grep -q "DRY-RUN: systemctl enable --now runners-pull.timer" <<< "$out"; expect $? "dry-run plans the self-update service and timer"
grep -qF "OPERATOR ACTION" <<< "$out" && grep -qF "ssh-keygen -t ed25519 -N '' -C runners-box -f $S/etc-runners/deploy_key" <<< "$out" \
  && grep -qF "ssh root@worker-1 cat $S/etc-runners/deploy_key.pub > runners-box.pub && gh repo deploy-key add runners-box.pub --repo <owner>/<repository> --title runners-box" <<< "$out"
expect $? "without the deploy key the gate prints how to make one and add it read-only"
grep -qF "read-only access to its values repository (git@git.example.invalid:someone/lane-values.git, cloned at $S/config)" <<< "$out" && grep -qF "Until then runners-pull.service cannot fetch $S/config." <<< "$out"
expect $? "the gate names the values repository by the origin of the machine's values checkout, and where that checkout is"
out="$(HOST_YML_UNDER_TEST="$S/config/hosts/box.yml" RUNNERS_CONFIG_UNDER_TEST="$S/no-checkout" run_host 2>&1)"
grep -qF "its values repository (the private repository whose root holds hosts/box.yml; no checkout at $S/no-checkout names it yet, so clone it there once the key is added)" <<< "$out"
expect $? "without a values checkout the gate says which repository it means and where to clone it"
out="$(run_host 2>&1)"
grep -qF "echo box > $S/runners-host" <<< "$out"; expect $? "without /etc/runners-host naming this host the gate prints the command"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; } && [ -z "$(ls -A "$S/units")" ] && [ ! -e "$S/etc-docker/daemon.json" ] && [ ! -e "$IMG" ]; expect $? "dry-run executes no mutation and writes no file"
{ ! grep -qE -- '-ci@|\.slice|listener|store\.mount' <<< "$out"; }; expect $? "the host provisioner plans nothing of any lane"
out="$(HOST_PKGS_ABSENT="python3-jwt" run_host 2>&1)"
grep -q "DRY-RUN: env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-jwt" <<< "$out"; expect $? "a missing host package is planned for install, alone"

# ---- apply from nothing -----------------------------------------------------------------------------
setup
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--apply provisions a host from nothing"
[ "$(jq -r .bip "$S/etc-docker/daemon.json")" = "172.17.0.1/16" ]; expect $? "daemon.json bip is docker0's live address"
[ "$(jq -c '.["default-address-pools"]' "$S/etc-docker/daemon.json")" = "$POOLS" ]; expect $? "daemon.json pools are Docker 29's built-in pools, verbatim"
grep -q "^dockerd --validate --config-file " "$CALLLOG"; expect $? "dockerd validates the merged daemon.json before it is written"
u="$S/units/var-lib-sysbox.mount"
grep -qx "Where=$S/sysbox" "$u" && grep -qx "What=$IMG" "$u" && grep -qx "Before=sysbox-mgr.service" "$u" && grep -qx "Options=loop,discard" "$u" && grep -qx "RequiresMountsFor=$S/imgdir" "$u"
expect $? "the mount unit mounts hosts.sysbox.image on Sysbox's data directory before sysbox-mgr, after the image's own filesystem"
grep -qx "truncate -s 20G $IMG" "$CALLLOG" && grep -qx "mkfs.ext4 -q -F -m 0 $IMG" "$CALLLOG"; expect $? "a missing Sysbox image is made sparse, ext4, at the configured size"
grep -qx "RequiresMountsFor=$S/sysbox" "$S/units/sysbox-mgr.service.d/known-ci-storage.conf"; expect $? "sysbox-mgr requires that mount (the drop-in keeps its legacy name, which machines already running carry)"
before '^dockerd --validate' '^apt-get install -y .*\.deb' && before '^systemctl enable --now var-lib-sysbox.mount' '^apt-get install -y .*\.deb'; expect $? "daemon.json and the bounded filesystem converge before Sysbox is installed"
grep -q "^curl -fsSL --retry 3 -o .* https://github.com/nestybox/sysbox/releases/download/v0.7.1/sysbox-ce_0.7.1.linux_amd64.deb token=none$" "$CALLLOG"; expect $? "downloads the pinned Sysbox release"
grep -qE "^systemctl show docker -p ExecMainStartTimestampMonotonic --value$" "$CALLLOG"; expect $? "checks Docker's start time around the install"
grep -qx "apt-mark hold sysbox-ce docker-ce-cli containerd.io linux-image-virtual" "$CALLLOG"; expect $? "holds Sysbox, Docker, containerd and the kernel meta (docker-ce was already held)"
{ ! grep -q "linux-generic" "$CALLLOG"; }; expect $? "a kernel meta that is not installed is not held"
{ ! grep -q "docker-ce=" "$CALLLOG" && ! grep -q "/linux/ubuntu/gpg" "$CALLLOG"; }; expect $? "Docker CE already installed is left alone (Phase A2 installs it only where it is absent)"
ps="$S/units/runners-pull.service"; pt="$S/units/runners-pull.timer"
grep -qx "ExecStart=$S/engine/lanes/bin/runners-pull.sh" "$ps" && grep -qx "Type=oneshot" "$ps" && ! grep -q '^User=' "$ps" && ! grep -q '^Environment' "$ps"; expect $? "the self-update runs the kit's bin/runners-pull.sh from the machine's engine checkout, as root, oneshot, with no environment of its own"
grep -qx "Description=runners: update $S/engine and $S/config from origin/main and converge this machine" "$ps"; expect $? "the self-update unit names both checkouts it keeps on origin/main"
grep -qx "OnBootSec=2min" "$pt" && grep -qx "OnUnitActiveSec=5min" "$pt" && grep -qx "WantedBy=timers.target" "$pt" && grep -qx "systemctl enable --now runners-pull.timer" "$CALLLOG"; expect $? "its timer fires every 5 minutes and is enabled"
before '^apt-mark hold' '^systemctl enable --now runners-pull.timer'; expect $? "the self-update is enabled after the host phases"
[ -d "$S/etc-runners" ] && [ "$(mode_of "$S/etc-runners")" = 700 ]; expect $? "the deploy key's directory exists, root-only, to take the key"
{ ! ls "$S/units" | grep -qE -- '-ci@|\.slice$|listener|token|image-build'; }; expect $? "the host provisioner writes no lane unit"
{ ! grep -qE '^docker network (create|rm)' "$CALLLOG"; }; expect $? "the host provisioner makes no lane network"

# ---- a second apply converges without repeating anything ------------------------------------------
: > "$CALLLOG"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "repeat --apply exits 0"
{ ! grep -qE '^(dockerd|truncate|mkfs|curl|apt-get|apt-mark|systemctl daemon-reload|systemctl enable --now .*\.mount|mount )' "$CALLLOG"; }; expect $? "repeat --apply writes, formats, downloads, installs, holds, reloads and mounts nothing"
grep -q "converged (bip=172.17.0.1/16, default-address-pools present)" <<< "$out" && grep -q "installed (0.7.1.linux)" <<< "$out" && grep -q "$IMG present" <<< "$out"; expect $? "repeat --apply reports daemon.json, Sysbox and its image converged"

# ---- adoption: a machine whose Sysbox filesystem an earlier provisioner made ----------------------
setup
printf 'image bytes\n' > "$IMG"; printf '0.7.1.linux\n' > "$S/st/sysbox"; echo "$S/sysbox" >> "$S/st/mounts"
printf '{"bip": "172.17.0.1/16", "default-address-pools": %s}\n' "$POOLS" > "$S/etc-docker/daemon.json"
printf '# Managed by an earlier provisioner\n[Mount]\nWhat=%s\nWhere=%s\nType=ext4\nOptions=loop,discard\n' "$IMG" "$S/sysbox" > "$S/units/var-lib-sysbox.mount"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(cat "$IMG")" = "image bytes" ] && ! grep -qE "^(truncate|mkfs)" "$CALLLOG"; expect $? "an existing Sysbox image at hosts.sysbox.image is adopted as it is: nothing made or formatted"
grep -qx "What=$IMG" "$S/units/var-lib-sysbox.mount" && grep -qx "mount -o remount,discard $S/sysbox" "$CALLLOG" && ! grep -q "systemctl enable --now var-lib-sysbox.mount" "$CALLLOG"
expect $? "its mount unit is rewritten and the live mount remounted in place, never restarted under sysbox-mgr"
{ ! grep -qE '^(apt-get|curl|dockerd)' "$CALLLOG"; }; expect $? "an adopted host installs, downloads and validates nothing again"

# ---- a changed mount unit is applied to the live mount without unmounting it ----------------------
setup
run_host --apply >/dev/null 2>&1
sed -i 's/^Options=loop,discard$/Options=loop/' "$S/units/var-lib-sysbox.mount"; : > "$CALLLOG"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "Options=loop,discard" "$S/units/var-lib-sysbox.mount" && grep -qx "systemctl daemon-reload" "$CALLLOG" \
  && grep -qx "mount -o remount,discard $S/sysbox" "$CALLLOG" && ! grep -q "systemctl enable --now var-lib" "$CALLLOG"
expect $? "an older mount unit is rewritten with discard and the live mount is remounted in place, not restarted"

# ---- refusals ---------------------------------------------------------------------------------------
setup
printf '{"bip": "10.9.0.1/24"}\n' > "$S/etc-docker/daemon.json"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "sets bip=10.9.0.1/24 but docker0 runs 172.17.0.1/16" <<< "$out" && ! grep -qE '^(apt-get|dockerd)' "$CALLLOG"; expect $? "a daemon.json bip that differs from docker0 is refused before Sysbox"
setup
printf '{"log-driver": "json-file", "log-opts": {"max-size": "10m"}}\n' > "$S/etc-docker/daemon.json"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(jq -r '."log-driver"' "$S/etc-docker/daemon.json")" = json-file ] && [ "$(jq -r '."log-opts"."max-size"' "$S/etc-docker/daemon.json")" = 10m ] && compgen -G "$S/etc-docker/daemon.json.pre-runners.*" >/dev/null; expect $? "existing daemon.json keys are kept and the old file is backed up"
setup
out="$(DOCKERD_RC=1 run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && [ ! -e "$S/etc-docker/daemon.json" ] && ! grep -q '^apt-get' "$CALLLOG"; expect $? "a daemon.json dockerd rejects is never written"
setup
sed -i "s/^SYSBOX_SHA256=.*/SYSBOX_SHA256=\"$(printf '0%.0s' $(seq 1 64))\"/" "$S/repo/bin/provision-host.sh"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "does not match the pinned" <<< "$out" && ! grep -q '^apt-get' "$CALLLOG"; expect $? "a package that misses the pinned checksum is never installed"
setup
out="$(DOCKER_RESTARTS=1 run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "Docker restarted during the Sysbox install" <<< "$out" && ! grep -q 'runners-pull' "$CALLLOG"; expect $? "a Docker restart during the install stops the run loudly"
setup
printf '0.6.4-0\n' > "$S/st/sysbox"; echo "$S/sysbox" >> "$S/st/mounts"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "sysbox-ce 0.6.4-0 is installed; this kit pins 0.7.1" <<< "$out"; expect $? "another installed Sysbox version is refused"
setup
printf '0.7.1.linux\n' > "$S/st/sysbox"
out="$(run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "is not a mount; mounting over it would hide live Sysbox data" <<< "$out" && ! grep -qE '^(truncate|mkfs|systemctl enable)' "$CALLLOG"; expect $? "an installed Sysbox without the bounded filesystem is refused before any mount"
setup
out="$(run_host --check --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "do not combine it" <<< "$out"; expect $? "--check refuses --apply"
out="$(env PATH="$S/pathbin:$PATH" RUNNERS_ALLOW_NON_ROOT=1 bash "$S/repo/bin/provision-host.sh" 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && grep -q "usage:" <<< "$out"; expect $? "no host argument prints the usage and exits 2"
out="$(HOST_UNDER_TEST=nowhere run_host 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no host config at $S/config/hosts/nowhere.yml" <<< "$out"; expect $? "a host the values checkout has no hosts/<host>.yml for is refused"
out="$(HOST_UNDER_TEST=nowhere HOST_YML_UNDER_TEST="$S/nowhere.yml" run_host 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "no host config at $S/nowhere.yml" <<< "$out"; expect $? "a host file named outright that does not exist is refused too"
grep -qx 'ENGINE="${RUNNERS_ENGINE:-/opt/runner-lanes}"' "$ROOT/bin/provision-host.sh" && grep -qx 'CONFIG="${RUNNERS_CONFIG:-/opt/runner-lanes-config}"' "$ROOT/bin/provision-host.sh" \
  && grep -qx 'CHECKOUT="$ENGINE/lanes"' "$ROOT/bin/provision-host.sh" && grep -qx 'HOST_YML="${RUNNERS_HOST_YML:-$CONFIG/hosts/$HOST.yml}"' "$ROOT/bin/provision-host.sh"
expect $? "on a machine the engine is /opt/runner-lanes with the kit under lanes/, and the host file is /opt/runner-lanes-config/hosts/<host>.yml"
printf 'hostname: x\n' > "$S/bad.yml"
out="$(HOST_YML_UNDER_TEST="$S/bad.yml" run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "invalid lane config" <<< "$out" && ! grep -qE '^(docker network|dockerd|truncate|curl)' "$CALLLOG"; expect $? "an invalid host config stops the run before any host phase"

# ---- Phase A2: Docker CE where it is absent, at the image's pins ---------------------------------
setup
out="$(DOCKER_CE_ABSENT=1 run_host --apply 2>&1)"; rc=$?
DOCKER_PIN="$(sed -n 's/^ARG DOCKER_VERSION=//p' "$ROOT/image/Dockerfile")"
CONTAINERD_PIN="$(sed -n 's/^ARG CONTAINERD_VERSION=//p' "$ROOT/image/Dockerfile")"
[ "$rc" -eq 0 ] && grep -qx "apt-get install -y docker-ce=$DOCKER_PIN docker-ce-cli=$DOCKER_PIN containerd.io=$CONTAINERD_PIN" "$CALLLOG"; expect $? "a host without Docker CE gets docker-ce, docker-ce-cli and containerd.io at the versions the runner image's Dockerfile pins"
grep -q "^curl -fsSL --retry 3 -o .* https://download.docker.com/linux/ubuntu/gpg token=none$" "$CALLLOG" && cmp -s "$S/apt/keyrings/docker.asc" "$S/fx/docker.asc" && [ "$(mode_of "$S/apt/keyrings/docker.asc")" = 644 ]; expect $? "Docker's apt key is downloaded, verified against the image's pin and installed as the keyring"
source_line="$(sed -n 's/^ *echo "\(deb \[arch=amd64 .*\)" \\$/\1/p' "$ROOT/image/Dockerfile")"
[ -n "$source_line" ] && [ "$(cat "$S/apt/sources.list.d/docker.list")" = "$source_line" ]; expect $? "the host's apt source is the Dockerfile's, verbatim"
before '^apt-get update' '^apt-get install -y docker-ce=' && before '^apt-get install -y docker-ce=' '^dockerd --validate' && before '^apt-get install -y docker-ce=' '^apt-get install -y .*\.deb'; expect $? "Docker is installed before daemon.json and Sysbox"
setup
printf 'another key\n' > "$S/fx/other.asc"
out="$(DOCKER_CE_ABSENT=1 GPG_FIXTURE="$S/fx/other.asc" run_host --apply 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "Docker's apt key sha256 .* does not match the image's pin" <<< "$out" && ! grep -q '^apt-get' "$CALLLOG" && [ ! -e "$S/apt/keyrings/docker.asc" ]; expect $? "a Docker key that misses the image's pin installs nothing"
setup
out="$(DOCKER_CE_ABSENT=1 run_host 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: install docker-ce=$DOCKER_PIN docker-ce-cli=$DOCKER_PIN containerd.io=$CONTAINERD_PIN from Docker's apt repository" <<< "$out" && ! grep -q '^curl\|^apt-get' "$CALLLOG"; expect $? "dry-run plans the Docker install without downloading anything"
setup
out="$(HOST_PKGS_ABSENT="jq python3-jwt python3-cryptography" run_host --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "apt-get install -y jq python3-jwt python3-cryptography" "$CALLLOG" && before '^apt-get install -y jq' '^dockerd --validate'; expect $? "missing host packages are installed first, before anything reads JSON"

# ---- check ------------------------------------------------------------------------------------------
setup
converged
out="$(run_host --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "--check passes on a converged host"
grep -q "host_packages=present" <<< "$out" && grep -q "packages=sysbox-ce:0.7.1.linux docker-ce:5:29.5.2" <<< "$out" && grep -q "missing_holds=none" <<< "$out" && grep -q "kernel_meta=linux-image-virtual" <<< "$out"; expect $? "--check reports package versions and holds"
grep -q "docker_runtime_sysbox=yes docker_storage=overlayfs daemon_json_bip=172.17.0.1/16 daemon_json_pools=present" <<< "$out" && grep -q "sysbox_services=sysbox:active sysbox-mgr:active sysbox-fs:active" <<< "$out"; expect $? "--check reports the runtime, the image store, daemon.json and the Sysbox services"
grep -q "sysbox_fs=mounted (/dev/loop9 ext4 20G)" <<< "$out" && grep -q "disk free=62G sysbox_image=$IMG (20G, sparse)" <<< "$out"; expect $? "--check reports the Sysbox mount and the disk headroom for its image"
grep -qx "self_update_timer=runners-pull.timer active" <<< "$out" && grep -q "deploy_key=$S/etc-runners/deploy_key present" <<< "$out" && grep -qx "runners_host=box" <<< "$out" && grep -qx "last_applied=engine=$(git -C "$S/engine" rev-parse HEAD) config=$(git -C "$S/config" rev-parse HEAD) (the HEADs of both checkouts)" <<< "$out"
expect $? "--check reports the self-update timer, the deploy key, the machine's host name and the two HEADs last applied, as runners-pull records them"
{ ! grep -qE "$MUTATIONS" "$CALLLOG"; }; expect $? "--check mutates nothing"
echo inactive > "$S/st/state/runners-pull.timer"
out="$(run_host --check 2>&1)"; rc=$?
echo active > "$S/st/state/runners-pull.timer"
[ "$rc" -ne 0 ] && grep -qx "self_update_timer=runners-pull.timer inactive" <<< "$out"; expect $? "--check fails when the self-update timer is not active"
rm "$S/etc-runners/deploy_key"
out="$(run_host --check 2>&1)"; rc=$?
printf 'key\n' > "$S/etc-runners/deploy_key"
[ "$rc" -ne 0 ] && grep -q "deploy_key=$S/etc-runners/deploy_key absent" <<< "$out"; expect $? "--check fails without the deploy key"
printf 'other\n' > "$S/runners-host"
out="$(run_host --check 2>&1)"; rc=$?
printf 'box\n' > "$S/runners-host"
[ "$rc" -ne 0 ] && grep -qx "runners_host=other" <<< "$out"; expect $? "--check fails when /etc/runners-host names another host config"
gitc -C "$S/config" commit -q --allow-empty -m two
out="$(run_host --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "last_applied=.* checkout_heads=engine=.* config=$(git -C "$S/config" rev-parse HEAD) (an apply is pending or failing: journalctl -u runners-pull.service)" <<< "$out"; expect $? "--check says when the values checkout's HEAD has not been applied yet, without failing on it"
gitc -C "$S/engine" commit -q --allow-empty -m two
printf 'engine=%s config=%s\n' "$(git -C "$S/engine" rev-parse HEAD~1)" "$(git -C "$S/config" rev-parse HEAD)" > "$S/pull-state/applied"
out="$(run_host --check 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "last_applied=.* checkout_heads=engine=$(git -C "$S/engine" rev-parse HEAD) config=.* (an apply is pending or failing" <<< "$out"; expect $? "and when the engine's has not"
printf 'docker-ce\n' > "$S/st/holds.partial"; cp "$S/st/holds" "$S/st/holds.full"; cp "$S/st/holds.partial" "$S/st/holds"
out="$(run_host --check 2>&1)"; rc=$?
cp "$S/st/holds.full" "$S/st/holds"
[ "$rc" -ne 0 ] && grep -q "missing_holds=sysbox-ce docker-ce-cli containerd.io linux-image-virtual" <<< "$out"; expect $? "--check fails when the packages are not held"
grep -vx "$S/sysbox" "$S/st/mounts" > "$S/st/mounts.new"; cp "$S/st/mounts" "$S/st/mounts.full"; mv "$S/st/mounts.new" "$S/st/mounts"
out="$(run_host --check 2>&1)"; rc=$?
cp "$S/st/mounts.full" "$S/st/mounts"
[ "$rc" -ne 0 ] && grep -q "sysbox_fs=not-mounted" <<< "$out"; expect $? "--check fails when /var/lib/sysbox is not mounted"
printf '# edited on the machine\n' >> "$S/units/var-lib-sysbox.mount"
out="$(run_host --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "sysbox_mount_unit=var-lib-sysbox.mount absent or DRIFTED" <<< "$out"; expect $? "--check fails on a mount unit that drifted from the checkout"
out="$(NO_RUNTIME=1 run_host --check 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "docker_runtime_sysbox=no" <<< "$out"; expect $? "--check fails when Docker lists no sysbox-runc runtime"

# ---- remove -----------------------------------------------------------------------------------------
setup
converged
out="$(run_host --remove 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "DRY-RUN: systemctl disable --now runners-pull.timer" <<< "$out" && { ! grep -qE "$MUTATIONS" "$CALLLOG"; } && [ -f "$S/units/runners-pull.timer" ]; expect $? "--remove alone plans the teardown and mutates nothing"
out="$(run_host --remove --apply 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "systemctl disable --now runners-pull.timer" "$CALLLOG" && [ ! -e "$S/units/runners-pull.timer" ] && [ ! -e "$S/units/runners-pull.service" ] && [ ! -e "$S/pull-state" ]
expect $? "--remove --apply disables the self-update and deletes its units and record"
[ -f "$S/units/var-lib-sysbox.mount" ] && [ -f "$S/etc-docker/daemon.json" ] && [ -f "$S/etc-runners/deploy_key" ] && ! grep -qE '^(apt-get|umount)' "$CALLLOG" \
  && grep -q "Left in place: Docker, sysbox-ce and var-lib-sysbox.mount ($IMG), the daemon.json keys, the apt holds, the deploy key" <<< "$out"
expect $? "--remove keeps Docker, Sysbox, its filesystem, daemon.json, the holds and the deploy key, and says so"

# ---- the kit's own host files: the gates name each one's machine ------------------------------------
setup
out="$(HOST_UNDER_TEST=example HOST_YML_UNDER_TEST="$ROOT/examples/hosts/example.yml" run_host 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "Phase C: $S/sysbox on its own 20G filesystem (/var/lib/runners/sysbox.img)" <<< "$out" && grep -qF "echo example > $S/runners-host" <<< "$out"
expect $? "the example's Sysbox filesystem is the image its host file names, and its gate names the example host config"
setup
out="$(HOST_UNDER_TEST=vps-1 HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_host 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q "Phase C: $S/sysbox on its own 40G filesystem (/var/lib/orbit-ci/sysbox.img)" <<< "$out" && grep -qF "echo vps-1 > $S/runners-host" <<< "$out" \
  && grep -qF "ssh root@vps-1 cat " <<< "$out"
expect $? "vps-1's Sysbox filesystem is the 40G image under its lane's state directory, and its gates name the vps-1 host config and ssh target"

echo
echo "provision-host-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
