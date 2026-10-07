# shellcheck shell=bash
# provision.sh: what bin/provision-host.sh and bin/provision-lane.sh share. Sourced, never run.
#
# Callers set APPLY (1 to mutate, else dry-run) before calling run_mutation or converge_file, and
# REPO_ROOT (the kit's root, the directory holding bin/ and lib/) before calling read_settings.

log()  { printf '\033[1;36m[runners]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[runners WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[runners FAIL]\033[0m %s\n' "$*" >&2; exit 2; }
gate() { printf '\033[1;35m[OPERATOR ACTION]\033[0m %s\n' "$*"; }

# Sysbox CE v0.7.1 (2026-07-31), the first release that supports Ubuntu 24.04 on kernel 6.8 under
# containerd 2.x. bin/provision-host.sh installs it (its package checksum is pinned there); a lane
# refuses to provision without it.
SYSBOX_VERSION="0.7.1"

# True when dpkg's version for sysbox-ce is the pin: bare, Debian-revisioned, or Nestybox's own
# <pin>.linux spelling (dpkg reports 0.7.1.linux; the raw comparison refused it, 2026-09-26).
sysbox_version_pinned() {
  case "$1" in "$SYSBOX_VERSION"|"$SYSBOX_VERSION"-*|"$SYSBOX_VERSION".linux*) return 0 ;; *) return 1 ;; esac
}

installed_version() {
  dpkg-query -W -f='${Version}' "$1" 2>/dev/null || true
}

run_mutation() {
  if [ "$APPLY" = "1" ]; then
    log "RUN: $*"
    "$@"
  else
    log "DRY-RUN: $*"
  fi
}

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

hash_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'; else shasum -a 256 | awk '{print $1}'; fi
}

# Write $2 to $1 (mode $3, default 0644) when the content differs. Prints "changed" when it wrote
# (or would write).
converge_file() {
  local dest="$1" content="$2" mode="${3:-0644}" tmp
  if [ -f "$dest" ] && [ "$(cat "$dest")" = "$content" ]; then
    return 0
  fi
  if [ "$APPLY" != "1" ]; then
    log "DRY-RUN: write $dest" >&2
    echo changed
    return 0
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$content" > "$tmp"
  # Only when absent: install -d resets an existing directory's mode, and /etc/<lane> is 0700.
  [ -d "$(dirname "$dest")" ] || install -d -m 0755 "$(dirname "$dest")"
  install -m "$mode" "$tmp" "$dest"
  rm -f "$tmp"
  log "wrote $dest" >&2
  echo changed
}

# A directory created (mode $2) only when absent: a configured path's parent may be a system
# directory, or another lane's state directory, whose mode is not this script's to set.
ensure_dir() {
  [ -d "$1" ] || run_mutation install -d -m "$2" "$1"
}

unit_running() { case "$1" in active|activating|deactivating|reloading|refreshing) return 0 ;; *) return 1 ;; esac; }

# systemd names a mount unit after its path: each / becomes -, a - inside a component \x2d.
mount_unit_name() { local path="${1#/}"; path="${path//-/\\x2d}"; printf '%s.mount' "${path//\//-}"; }

# Load lib/lanes.py's KEY=value lines (`host-env <host.yml>` or `env <host.yml> <lane>`) as shell
# variables. Every value has passed lanes.py's validation; a line that is not KEY=value is refused.
read_settings() {
  local text key value
  text="$(python3 "$REPO_ROOT/lib/lanes.py" "$@")" || die "invalid lane config: python3 lib/lanes.py $* (needs python3-yaml)"
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^[A-Z_]+$ ]] || die "unexpected setting line from lib/lanes.py: $key"
    printf -v "$key" '%s' "$value"
  done <<< "$text"
}

# One --apply at a time per machine: an operator's run and runners-pull.service's must not
# interleave their writes. Held on fd 8 until the script exits.
take_apply_lock() {
  [ "$APPLY" = "1" ] || return 0
  exec 8>"${RUNNERS_APPLY_LOCK:-/run/runners-apply.lock}"
  flock -w 1800 8 || die "another --apply has held ${RUNNERS_APPLY_LOCK:-/run/runners-apply.lock} for 30 minutes"
}
