# shellcheck shell=bash
# stubs.sh: the system tools the provisioning tests stub on PATH. Sourced by the tests, never run.
#
# write_stubs <dir> writes docker, dockerd, apt-get, apt-mark, dpkg-query, systemctl, journalctl,
# useradd, getent, chown, chgrp, chmod, runuser, stat, mountpoint, mount, umount, mkfs.ext4,
# mkfs.xfs, mkfs.btrfs, blkid, btrfs, stub-image, cp, truncate, findmnt, nproc, df, ip, nft,
# iptables, timeout, sleep, curl and gh into <dir>. No real package, mount, unit, container, firewall, network or GitHub call happens. They
# record every mutation in $CALLLOG and keep their state under $ST; fixtures come from $FX. A
# mount unit's enable mounts its Where= path; GNU stat -c %u reads a list of root-owned paths;
# cp --reflink=always is done as a plain copy (neither ext4 nor tmpfs takes one); chmod
# --reference is done from the reference's mode. A filesystem's type is what mkfs left as the first
# line of its image, and a mount records it for its mount point in $ST/mount-types; stat -f, findmnt
# and blkid answer from there, or STORE_FSTYPE (xfs when unset) for a store nothing mounted. The
# files of an image a mkfs stand-in made follow it: they are in its mount point while it is mounted
# and nowhere a test's lane looks while it is not (stub-image), so a store that is unmounted, has
# its image renamed and is mounted elsewhere shows there what it held. btrfs
# keeps subvolumes as directories whose inode it lists in $ST/subvolumes, so one stays a subvolume
# when it is renamed; a snapshot is a plain copy, and a delete follows a link as the real one does. The gh stub applies the script's own jq filter to
# its fixture with the real jq.

write_stubs() {
  local p="$1"
  mkdir -p "$p"
  cat > "$p/docker" <<'SH'
#!/bin/bash
echo "docker $*" >> "$CALLLOG"
case "$1 $2" in
  "network inspect")
    if [ "$3" = bridge ]; then echo "172.17.0.1 172.17.0.0/16"; exit 0; fi
    [ -f "$ST/network-$3" ] || exit 1
    case "$*" in
      *enable_icc*) echo "$(cat "$ST/network-$3") ${ICC:-false} 172.20.0.0/16 172.20.0.1" ;;
      *bridge.name*) cat "$ST/network-$3" ;;
    esac
    exit 0 ;;
  "network create")
    for a in "$@"; do case "$a" in com.docker.network.bridge.name=*) echo "${a#*=}" > "$ST/network-${*: -1}" ;; esac; done
    exit 0 ;;
  "network rm") rm -f "$ST/network-$3"; exit 0 ;;
  "image inspect")
    [ "${IMAGE_PRESENT:-1}" = 1 ] || exit 1
    case " ${IMAGES_ABSENT:-} " in *" ${*: -1} "*) exit 1 ;; esac
    case "$*" in *--format*) echo "2026-09-25T00:00:00Z" ;; esac
    exit 0 ;;
  "info --format")
    case "$*" in
      *Runtimes*) if [ -f "$ST/sysbox" ] && [ "${NO_RUNTIME:-0}" != 1 ]; then echo '{"runc":{},"sysbox-runc":{}}'; else echo '{"runc":{}}'; fi ;;
      *Driver*) echo overlayfs ;;
    esac
    exit 0 ;;
  "run --rm") cat > "$ST/smoke-stdin"; cat "${SMOKE_OUT:-$FX/smoke.out}"; exit 0 ;;
esac
exit 0
SH
  cat > "$p/dockerd" <<'SH'
#!/bin/bash
echo "dockerd $*" >> "$CALLLOG"
exit "${DOCKERD_RC:-0}"
SH
  cat > "$p/apt-get" <<'SH'
#!/bin/bash
echo "apt-get $*" >> "$CALLLOG"
# The Nestybox deb reports its version as 0.7.1.linux (dpkg-query on the machine, 2026-09-26).
case "$*" in *.deb*) [ "${APT_RC:-0}" = 0 ] && printf '0.7.1.linux\n' > "$ST/sysbox" ;; esac
case "$*" in *docker-ce=*) [ "${APT_RC:-0}" = 0 ] && touch "$ST/docker-ce" ;; esac
[ "${DOCKER_RESTARTS:-0}" = 1 ] && touch "$ST/docker-restarted"
exit "${APT_RC:-0}"
SH
  cat > "$p/apt-mark" <<'SH'
#!/bin/bash
if [ "$1" = showhold ]; then cat "$ST/holds"; exit 0; fi
echo "apt-mark $*" >> "$CALLLOG"
shift
printf '%s\n' "$@" >> "$ST/holds"
SH
  cat > "$p/dpkg-query" <<'SH'
#!/bin/bash
case "$2" in
  *Status-Abbrev*) printf 'ii |linux-image-virtual\n'; printf 'un |linux-generic\n'; exit 0 ;;
esac
case "$3" in
  sysbox-ce) [ -f "$ST/sysbox" ] && { cat "$ST/sysbox"; exit 0; }; exit 1 ;;
  docker-ce|docker-ce-cli)
    if [ "${DOCKER_CE_ABSENT:-0}" = 1 ] && [ ! -f "$ST/docker-ce" ]; then exit 1; fi
    echo "5:29.5.2-1~ubuntu.24.04~noble" ;;
  containerd.io) echo "2.2.4-1~ubuntu.24.04~noble" ;;
  linux-image-virtual) echo "6.8.0-142.142" ;;
  jq|python3-yaml|python3-jwt|python3-cryptography|btrfs-progs) case " ${HOST_PKGS_ABSENT:-} " in *" $3 "*) exit 1 ;; esac; echo "1.0" ;;
  *) exit 1 ;;
esac
SH
  # systemctl: unit states live in $ST/state/<unit> (the Sysbox services are active unless
  # ACTIVE_STATE says otherwise; any other unit is inactive until started). Starting a listener
  # with WARM_POOL=1 starts a warm slot the way the listener would.
  cat > "$p/systemctl" <<'SH'
#!/bin/bash
echo "systemctl $*" >> "$CALLLOG"
# A mount unit mounts its Where= path as its Type=, and fails on an image mkfs made as another type.
mount_unit() {
  local where what type made
  [ "${MOUNT_FAIL:-0}" = 1 ] && return 1
  where="$(sed -n 's/^Where=//p' "$UNITS/$1")"; what="$(sed -n 's/^What=//p' "$UNITS/$1")"; type="$(sed -n 's/^Type=//p' "$UNITS/$1")"
  made="$(head -n 1 "$what" 2>/dev/null)"
  case "$made" in xfs|btrfs) [ "$made" = "$type" ] || { echo "mount: $where: wrong fs type ($what is $made, the unit says $type)" >&2; return 32; } ;; esac
  echo "$where" >> "$ST/mounts"; echo "$where $type" >> "$ST/mount-types"
  "$(dirname "$0")/stub-image" attach "$what" "$where"
}
# MOUNT_STOP_RC fails the unmount, as a mount something still uses does: it stays mounted.
unmount_unit() {
  local where
  [ "${MOUNT_STOP_RC:-0}" = 0 ] || { echo "umount: target is busy" >&2; return "$MOUNT_STOP_RC"; }
  where="$(sed -n 's/^Where=//p' "$UNITS/$1")"
  "$(dirname "$0")/stub-image" detach "$where"
  grep -vx -- "$where" "$ST/mounts" > "$ST/mounts.new"; mv "$ST/mounts.new" "$ST/mounts"
  grep -v -- "^$where " "$ST/mount-types" > "$ST/mount-types.new" 2>/dev/null; mv "$ST/mount-types.new" "$ST/mount-types"
}
state_of() {
  if [ -f "$ST/state/$1" ]; then cat "$ST/state/$1"; return; fi
  case "$1" in sysbox|sysbox-mgr|sysbox-fs) echo "${ACTIVE_STATE:-active}" ;; *) echo inactive ;; esac
}
case "$1" in
  show)
    case "$2" in
      docker) if [ -f "$ST/docker-restarted" ]; then echo 999; else echo 100; fi ;;
      *.slice)
        case "$*" in
          *--value*) echo "${TOP_WEIGHT:-50}" ;;
          *) printf 'MemoryMax=%s\nMemoryHigh=%s\nCPUQuotaPerSecUSec=8s\nCPUWeight=%s\n' "${SLICE_MEM:-17179869184}" "${SLICE_HIGH:-15032385536}" "${SLICE_WEIGHT:-50}" ;;
        esac ;;
    esac ;;
  is-active) for u in "${@:2}"; do state_of "$u"; done ;;
  is-enabled) echo "${ENABLED_STATE:-enabled}" ;;
  start|restart)
    for u in "${@:2}"; do
      case "$u" in *.mount) mount_unit "$u" || exit 1 ;; esac
      echo active > "$ST/state/$u"
      case "$u" in
        # The token refresh writes the lane user's hosts.yml, as bin/lane-token-refresh.py does.
        *-token-refresh.service)
          out="$(sed -n 's/.* --hosts-out \([^ ]*\) .*/\1/p' "$UNITS/$u")"
          [ -z "$out" ] || { mkdir -p "$(dirname "$out")"; printf 'github.com:\n    oauth_token: fixture-refreshed-token\n' > "$out"; } ;;
        *-listener.service)
          if [ "${WARM_POOL:-0}" = 1 ]; then
            mkdir -p "$RUN_T/ci/2"; printf 'acme ci 9 7002 box-lane-ci-2-1727005000\n' > "$RUN_T/ci/2/job"
            echo "${WARM_STATE:-active}" > "$ST/state/box-ci-ci@2.service"
          fi ;;
      esac
    done ;;
  stop)
    for u in "${@:2}"; do
      case "$u" in *.mount) unmount_unit "$u" || exit 1 ;; esac
      echo inactive > "$ST/state/$u"
    done ;;
  enable)
    for u in "$@"; do
      case "$u" in
        *.mount) mount_unit "$u" || exit 1 ;;
        *.service|*.timer)
          mkdir -p "$UNITS/multi-user.target.wants"; ln -sf "$UNITS/x" "$UNITS/multi-user.target.wants/$u"
          case " $* " in *" --now "*) echo active > "$ST/state/$u" ;; esac ;;
      esac
    done ;;
  disable)
    for u in "$@"; do
      rm -f "$UNITS/multi-user.target.wants/$u"
      case " $* " in *" --now "*) case "$u" in *.service|*.timer) echo inactive > "$ST/state/$u" ;; esac ;; esac
    done ;;
esac
exit 0
SH
  cat > "$p/journalctl" <<'SH'
#!/bin/bash
[ "${JOURNAL_TS:-now}" = none ] && exit 0
ts="${JOURNAL_TS:-now}"; [ "$ts" = now ] && ts="$(date +%s)"
echo "$ts.123456 box-1 listener[42]: heartbeat: 1 scale sets served, 1 of 2 slots in use"
SH
  cat > "$p/useradd" <<'SH'
#!/bin/bash
echo "useradd $*" >> "$CALLLOG"
echo "${@: -1}" >> "$ST/users"
SH
  cat > "$p/getent" <<'SH'
#!/bin/bash
[ "$1" = passwd ] && grep -qx -- "$2" "$ST/users" 2>/dev/null && echo "$2:x:998:998::/nonexistent:/usr/sbin/nologin"
SH
  cat > "$p/chown" <<'SH'
#!/bin/bash
echo "chown $*" >> "$CALLLOG"
case "$*" in
  *--reference=*) grep -vxF -- "${@: -1}" "$ST/root-owned" > "$ST/root-owned.new" 2>/dev/null; mv "$ST/root-owned.new" "$ST/root-owned" ;;
esac
exit 0
SH
  # runuser -u <user> -- <command>: runs it, marking the user it runs as for the gh stub.
  cat > "$p/runuser" <<'SH'
#!/bin/bash
echo "runuser $1 $2" >> "$CALLLOG"
user="$2"; shift 3
RUNUSER_AS="$user" exec "$@"
SH
  # GNU stat -c %u: 0 for a path listed in $ST/root-owned, 1000 for any other that exists.
  cat > "$p/stat" <<'SH'
#!/bin/bash
# stat -f -c %T <path>: the type of the filesystem mounted there.
if [ "$1" = -f ] && [ "$2" = -c ] && [ "$3" = %T ]; then
  [ -e "$4" ] || exit 1
  type="$(awk -v m="$4" '$1 == m { t = $2 } END { print t }' "$ST/mount-types" 2>/dev/null)"
  echo "${type:-${STORE_FSTYPE:-xfs}}"
  exit 0
fi
[ "$1" = -c ] && [ "$2" = %u ] && [ -e "$3" ] || exit 1
if grep -qxF -- "$3" "$ST/root-owned" 2>/dev/null; then echo 0; else echo 1000; fi
SH
  cat > "$p/chgrp" <<'SH'
#!/bin/bash
echo "chgrp $*" >> "$CALLLOG"
SH
  cat > "$p/chmod" <<'SH'
#!/bin/bash
if [[ "${1:-}" == --reference=* ]]; then
  mode="$(python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o7777, "o"))' "${1#--reference=}")" || exit 1
  shift
  exec /bin/chmod "$mode" "$@"
fi
exec /bin/chmod "$@"
SH
  cat > "$p/mountpoint" <<'SH'
#!/bin/bash
grep -qx -- "${@: -1}" "$ST/mounts"
SH
  cat > "$p/mount" <<'SH'
#!/bin/bash
echo "mount $*" >> "$CALLLOG"
[ "${MOUNT_RO_RC:-0}" = 0 ] || case "$*" in *loop,ro*) echo "mount: wrong fs type, bad superblock" >&2; exit "$MOUNT_RO_RC" ;; esac
echo "${@: -1}" >> "$ST/mounts"
image="${*: -2:1}"
case "$(head -n 1 "$image" 2>/dev/null)" in xfs|btrfs) echo "${@: -1} $(head -n 1 "$image")" >> "$ST/mount-types" ;; esac
"$(dirname "$0")/stub-image" attach "$image" "${@: -1}"
exit 0
SH
  cat > "$p/umount" <<'SH'
#!/bin/bash
echo "umount $*" >> "$CALLLOG"
"$(dirname "$0")/stub-image" detach "${@: -1}"
grep -vx -- "${@: -1}" "$ST/mounts" > "$ST/mounts.new"; mv "$ST/mounts.new" "$ST/mounts"
grep -v -- "^${*: -1} " "$ST/mount-types" > "$ST/mount-types.new" 2>/dev/null; mv "$ST/mount-types.new" "$ST/mount-types"
exit 0
SH
  cat > "$p/mkfs.ext4" <<'SH'
#!/bin/bash
echo "mkfs.ext4 $*" >> "$CALLLOG"
SH
  cat > "$p/mkfs.xfs" <<'SH'
#!/bin/bash
echo "mkfs.xfs $*" >> "$CALLLOG"
echo xfs > "${@: -1}"
"$(dirname "$0")/stub-image" format "${@: -1}"
SH
  cat > "$p/mkfs.btrfs" <<'SH'
#!/bin/bash
echo "mkfs.btrfs $*" >> "$CALLLOG"
[ "${MKFS_BTRFS_RC:-0}" = 0 ] || exit "$MKFS_BTRFS_RC"
echo btrfs > "${@: -1}"
"$(dirname "$0")/stub-image" format "${@: -1}"
SH
  cat > "$p/blkid" <<'SH'
#!/bin/bash
# blkid -p -o value -s TYPE <image>: what mkfs made it, xfs for an image no stand-in made, or
# BLKID_TYPE (empty: an image blkid finds nothing in).
[ -f "${@: -1}" ] || exit 2
if [ -n "${BLKID_TYPE+set}" ]; then [ -n "$BLKID_TYPE" ] || exit 2; echo "$BLKID_TYPE"; exit 0; fi
case "$(head -n 1 "${@: -1}" 2>/dev/null)" in btrfs) echo btrfs ;; *) echo xfs ;; esac
SH
  cat > "$p/stub-image" <<'SH'
#!/bin/bash
# Where the files of a filesystem image a mkfs stand-in made are (the header of stubs.sh):
#   stub-image attach <image> <dir>   a mount: the image's files appear in <dir>
#   stub-image detach <dir>           an unmount: they go back to the image mounted there
#   stub-image format <image>         a mkfs: the image holds no file
# They are kept by the image's inode, which a rename of the image keeps.
ino() { ls -di "$1" 2>/dev/null | awk '{ print $1 }'; }
move_all() { find "$1" -mindepth 1 -maxdepth 1 -exec mv {} "$2/" \; ; }
case "$1" in
  attach)
    case "$(head -n 1 "$2" 2>/dev/null)" in xfs|btrfs) ;; *) exit 0 ;; esac
    files="$ST/image-files/$(ino "$2")"
    echo "$3 $files" >> "$ST/mount-images"
    [ ! -d "$files" ] || move_all "$files" "$3" ;;
  detach)
    files="$(awk -v m="$2" '$1 == m { f = $2 } END { print f }' "$ST/mount-images" 2>/dev/null)"
    [ -n "$files" ] || exit 0
    mkdir -p "$files" && move_all "$2" "$files"
    grep -v -- "^$2 " "$ST/mount-images" > "$ST/mount-images.new"; mv "$ST/mount-images.new" "$ST/mount-images" ;;
  format) rm -rf "$ST/image-files/$(ino "$2")" ;;
esac
exit 0
SH
  cat > "$p/btrfs" <<'SH'
#!/bin/bash
echo "btrfs $*" >> "$CALLLOG"
ino() { ls -di "$1" 2>/dev/null | awk '{ print $1 }'; }
is_subvolume() { [ -d "$1" ] && [ ! -L "$1" ] && grep -qx -- "$(ino "$1")" "$ST/subvolumes" 2>/dev/null; }
[ "$1" = subvolume ] || exit 1
case "$2" in
  create)
    [ "${BTRFS_CREATE_RC:-0}" = 0 ] || exit "$BTRFS_CREATE_RC"
    [ ! -e "$3" ] || { echo "ERROR: target path already exists: $3" >&2; exit 1; }
    /bin/mkdir "$3" && ino "$3" >> "$ST/subvolumes" ;;
  snapshot)
    [ "${BTRFS_SNAPSHOT_RC:-0}" = 0 ] || { echo "ERROR: cannot snapshot '$3'" >&2; exit "$BTRFS_SNAPSHOT_RC"; }
    is_subvolume "$3" || { echo "ERROR: Not a Btrfs subvolume: $3" >&2; exit 1; }
    [ ! -e "$4" ] || { echo "ERROR: the stand-in refuses a destination that exists: $4" >&2; exit 1; }
    /bin/cp -a "$3" "$4" && ino "$4" >> "$ST/subvolumes" ;;
  delete)
    target="$3"; [ ! -L "$target" ] || target="$(readlink -f "$target")"
    [ "${BTRFS_DELETE_FAIL:-}" != "$target" ] || { echo "ERROR: cannot delete '$target'" >&2; exit 1; }
    is_subvolume "$target" || { echo "ERROR: Not a Btrfs subvolume: $target" >&2; exit 1; }
    grep -vx -- "$(ino "$target")" "$ST/subvolumes" > "$ST/subvolumes.new"; mv "$ST/subvolumes.new" "$ST/subvolumes"
    /bin/rm -rf "$target" ;;
  show) is_subvolume "$3" ;;
  *) exit 1 ;;
esac
SH
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
  cat > "$p/truncate" <<'SH'
#!/bin/bash
echo "truncate $*" >> "$CALLLOG"
: > "${@: -1}"
SH
  cat > "$p/findmnt" <<'SH'
#!/bin/bash
type="$(awk -v m="${*: -1}" '$1 == m { t = $2 } END { print t }' "$ST/mount-types" 2>/dev/null)"; type="${type:-${STORE_FSTYPE:-xfs}}"
case "$*" in *"-o FSTYPE"*) case "${@: -1}" in *store) echo "$type" ;; *) echo ext4 ;; esac; exit 0 ;; esac
case "${@: -1}" in *slot-*) echo " 10G" ;; *store) echo "/dev/loop8 $type 40G" ;; *) echo "/dev/loop9 ext4 20G" ;; esac
SH
  # The host's core count, for the per-slot cpusets.
  cat > "$p/nproc" <<'SH'
#!/bin/bash
echo "${NPROC:-16}"
SH
  cat > "$p/df" <<'SH'
#!/bin/bash
# Like the real df: a path that does not exist is an error, not a number.
echo "df ${@: -1}" >> "$CALLLOG"
[ -e "${@: -1}" ] || { echo "df: ${@: -1}: No such file or directory" >&2; exit 1; }
# The slot helper's reading of the store filesystem (size, free space, inodes, free inodes): half
# free, or the row STORE_DF_SHORT (a filesystem short of room). With STORE_DF_SHORT_WHILE that row is
# given only while that directory holds more than STORE_DF_SHORT_ABOVE entries, the way retired
# copies fill a store until they are deleted. STORE_DF_RC fails the reading.
case "$*" in
  *--output=size,avail,itotal,iavail*)
    [ "${STORE_DF_RC:-0}" = 0 ] || exit "$STORE_DF_RC"
    row="1000 500 1000 500"
    if [ -n "${STORE_DF_SHORT:-}" ]; then
      held="$(ls -A "${STORE_DF_SHORT_WHILE:-/nonexistent}" 2>/dev/null | grep -c .)"
      { [ -z "${STORE_DF_SHORT_WHILE:-}" ] || [ "$held" -gt "${STORE_DF_SHORT_ABOVE:-0}" ]; } && row="$STORE_DF_SHORT"
    fi
    printf '1B-blocks Avail Inodes IFree\n%s\n' "$row"
    exit 0 ;;
esac
printf ' Avail\n  %sG\n' "${DF_FREE:-62}"
SH
  cat > "$p/ip" <<'SH'
#!/bin/bash
case "$*" in
  *"route get"*) echo "1.1.1.1 via 172.31.1.1 dev eth0 src 203.0.113.7 uid 0" ;;
  *tailscale0*) echo "5: tailscale0    inet 100.64.0.9/32 scope global tailscale0" ;;
esac
SH
  # Both lane rules are present for every bridge in FW_BRIDGES unless FW_PRESENT=0.
  cat > "$p/nft" <<'SH'
#!/bin/bash
[ "${FW_PRESENT:-1}" = 1 ] || exit 0
for b in ${FW_BRIDGES:-box-ci0 own-ci0}; do
  echo "		iifname \"$b\" ip daddr { 10.0.0.0/8, 172.16.0.0/12 } counter packets 0 bytes 0 reject comment \"ai-fleet-known-ci\""
done
exit 0
SH
  cat > "$p/iptables" <<'SH'
#!/bin/bash
[ "${FW_PRESENT:-1}" = 1 ]
SH
  cat > "$p/timeout" <<'SH'
#!/bin/bash
shift
exec "$@"
SH
  cat > "$p/sleep" <<'SH'
#!/bin/bash
echo "sleep $*" >> "$CALLLOG"
SH
  # curl: the host provisioner's two downloads, and the slot helper's API calls. The helper hands
  # curl its bearer header as a config file on stdin; the stub checks it arrived there.
  cat > "$p/curl" <<'SH'
#!/bin/bash
args=("$@"); out=""; method=GET; token=none
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    -o) out="${args[$((i+1))]}" ;;
    -X) method="${args[$((i+1))]}" ;;
    --config) cfg="$(cat)"; case "$cfg" in *"Bearer ${EXPECTED_TOKEN:-}"*) token=match ;; *) token=mismatch ;; esac ;;
  esac
done
echo "curl $* token=$token" >> "$CALLLOG"
case "${@: -1}" in
  *.deb) [ "${CURL_FAIL:-0}" = 1 ] && exit 22; cp "$FX/sysbox.deb" "$out"; exit 0 ;;
  */linux/ubuntu/gpg) [ "${CURL_FAIL:-0}" = 1 ] && exit 22; cp "${GPG_FIXTURE:-$FX/docker.asc}" "$out"; exit 0 ;;
esac
if [ "$method" = POST ]; then
  printf '%s' "${MINT_BODY:-}" > "$out"
  printf '%s' "${MINT_STATUS:-201}"
else
  printf '%s' "${DELETE_STATUS:-204}"
fi
SH
  cat > "$p/gh" <<'SH'
#!/bin/bash
echo "gh $* [GH_CONFIG_DIR=${GH_CONFIG_DIR:-unset} GH_TOKEN=${GH_TOKEN:-unset} as=${RUNUSER_AS:-root}]" >> "$CALLLOG"
if [ "${GH_WRITES_CONFIG:-0}" = 1 ] && [ -d "${GH_CONFIG_DIR:-/nonexistent}" ] && [ ! -e "$GH_CONFIG_DIR/config.yml" ]; then
  : > "$GH_CONFIG_DIR/config.yml"
  [ -n "${RUNUSER_AS:-}" ] || echo "$GH_CONFIG_DIR/config.yml" >> "$ST/root-owned"
fi
args=("$@"); endpoint=""; filter=""; method=GET
for ((i = 1; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method="${args[$((i+1))]}"; i=$((i+1)) ;;
    --jq) filter="${args[$((i+1))]}"; i=$((i+1)) ;;
    --paginate|--silent) ;;
    *) [ -z "$endpoint" ] && endpoint="${args[$i]}" ;;
  esac
done
# A DELETE succeeds unless GH_DELETE_RC says otherwise (GitHub refuses to remove a runner on a job).
[ "$method" = DELETE ] && exit "${GH_DELETE_RC:-0}"
case "$endpoint" in
  */runner-groups) [ "${GH_GROUPS_RC:-0}" = 0 ] || exit 1; fixture="${GROUPS_JSON:-$FX/groups.json}" ;;
  installation/repositories) [ "${GH_RUNNERS_RC:-0}" = 0 ] || exit 1; fixture="$FX/install-repos.json" ;;
  */repositories) fixture="${REPOS_JSON:-$FX/repos.json}" ;;
  repos/*/actions/runners)
    repo="${endpoint#repos/}"; repo="${repo%/actions/runners}"
    fixture="$FX/runners-${repo//\//_}.json"; [ -f "$fixture" ] || fixture="$FX/runners-none.json" ;;
  */actions/runners) [ "${GH_RUNNERS_RC:-0}" = 0 ] || exit 1; fixture="${RUNNERS_JSON:-$FX/runners.json}" ;;
  *) exit 1 ;;
esac
jq -r "$filter" < "$fixture"
SH
  chmod +x "$p/"*
}
