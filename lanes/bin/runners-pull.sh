#!/usr/bin/env bash
# runners-pull.sh: keep this machine's two checkouts on their origin/main, and converge it.
#
# A machine has two checkouts. The engine is a clone of the catalog, whose lanes/ directory is this
# kit; it is public and fetched with no credential. The values checkout is the owner's private
# repository, whose root holds hosts/<host>.yml; it is fetched with the machine's read-only deploy
# key.
#
# runners-pull.service runs this as root every 5 minutes (runners-pull.timer, written by
# bin/provision-host.sh). One run:
#   1. takes a lock, so a run that outlives its tick never overlaps the next;
#   2. refuses, logging why and exiting non-zero, when either checkout has local modifications (a
#      hand edit on the machine; change the repository instead);
#   3. fetches the engine's origin and fast-forwards it to origin/main;
#   4. when that fast-forward changed this script, runs the new copy in its place, so a change to
#      what a run applies takes effect on the tick that pulled it;
#   5. fetches the values checkout's origin with the deploy key and fast-forwards it to origin/main;
#   6. when either HEAD moved, or no successful apply is recorded for these two HEADs, runs
#      bin/provision-host.sh <host> --apply, then bin/provision-lane.sh <host> <lane> --apply for
#      each lane of hosts/<host>.yml, then bin/provision-dashboards.sh <host> --apply, <host> being
#      the one line of /etc/runners-host;
#   7. records both HEADs as applied only when every one of them succeeded, so a failure is retried
#      at the next tick. A failed host apply skips the rest; a failed lane stops neither the other
#      lanes nor the dashboards.
#
# Linux-only by design: Ubuntu 24.04, bash 5, git, util-linux flock, OpenSSH.
set -euo pipefail

ENGINE="${RUNNERS_ENGINE:-/opt/runner-lanes}"
CONFIG="${RUNNERS_CONFIG:-/opt/runner-lanes-config}"
# This kit inside the engine checkout, and this script's path inside that repository.
KIT="$ENGINE/lanes"
SELF="lanes/bin/runners-pull.sh"
HOST_FILE="${RUNNERS_HOST_FILE:-/etc/runners-host}"
DEPLOY_KEY="${RUNNERS_DEPLOY_KEY:-/etc/runners/deploy_key}"
STATE_DIR="${RUNNERS_PULL_STATE_DIR:-/var/lib/runners}"
LOCK="${RUNNERS_PULL_LOCK:-/run/runners-pull.lock}"
APPLIED="$STATE_DIR/applied"

log() { printf 'runners-pull: %s\n' "$*"; }
die() { printf 'runners-pull: %s\n' "$*" >&2; exit 1; }

main() {
  local host checkout engine_before engine_head config_before config_head heads host_yml lanes lane failed=""
  exec 9>"$LOCK"
  if ! flock -n 9; then log "another run holds $LOCK; this tick does nothing"; return 0; fi
  host="$(tr -d '[:space:]' < "$HOST_FILE" 2>/dev/null || true)"
  [[ "$host" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "$HOST_FILE must hold this machine's host config name (hosts/<name>.yml), got '${host}'"
  [ -r "$DEPLOY_KEY" ] || die "no deploy key at $DEPLOY_KEY (bin/provision-host.sh prints how to add one)"
  for checkout in "$ENGINE" "$CONFIG"; do
    git -C "$checkout" rev-parse --git-dir >/dev/null 2>&1 \
      || die "$checkout is not a git checkout; nothing fetched or applied. The engine is a clone of the catalog at $ENGINE and the values repository is cloned at $CONFIG (README.md, Adding a machine)"
    if [ -n "$(git -C "$checkout" status --porcelain)" ]; then
      git -C "$checkout" status --short >&2
      die "$checkout has local modifications; nothing fetched or applied. Change the repository, not the machine: move the edits aside (git -C $checkout stash), then the next tick converges"
    fi
  done
  engine_before="$(git -C "$ENGINE" rev-parse HEAD)"
  config_before="$(git -C "$CONFIG" rev-parse HEAD)"
  # The engine is public: no key is offered for it.
  git -C "$ENGINE" fetch --quiet origin || die "git fetch failed in $ENGINE"
  git -C "$ENGINE" merge --ff-only --quiet origin/main || die "$ENGINE cannot fast-forward to origin/main"
  engine_head="$(git -C "$ENGINE" rev-parse HEAD)"
  if [ "$engine_head" != "$engine_before" ] && ! git -C "$ENGINE" diff --quiet "$engine_before" "$engine_head" -- "$SELF"; then
    # This run's own steps are the old copy's; the new copy sees these HEADs unapplied and applies them.
    log "$SELF changed in ${engine_before:0:12}..${engine_head:0:12}; running the new copy"
    exec "$KIT/bin/runners-pull.sh"
  fi
  GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
    git -C "$CONFIG" fetch --quiet origin || die "git fetch failed in $CONFIG"
  git -C "$CONFIG" merge --ff-only --quiet origin/main || die "$CONFIG cannot fast-forward to origin/main"
  config_head="$(git -C "$CONFIG" rev-parse HEAD)"
  # bin/provision-host.sh --check reads the record in this form.
  heads="engine=$engine_head config=$config_head"
  if [ "$engine_head" = "$engine_before" ] && [ "$config_head" = "$config_before" ] && [ "$(cat "$APPLIED" 2>/dev/null || true)" = "$heads" ]; then
    return 0
  fi
  log "engine ${engine_before:0:12} -> ${engine_head:0:12}, values ${config_before:0:12} -> ${config_head:0:12}; converging $host"
  if ! "$KIT/bin/provision-host.sh" "$host" --apply; then
    die "provision-host.sh $host --apply failed; the lanes were not applied and nothing is recorded (the next tick retries)"
  fi
  host_yml="${RUNNERS_HOST_YML:-$CONFIG/hosts/$host.yml}"
  lanes="$(python3 "$KIT/lib/lanes.py" lanes "$host_yml")" || die "$host_yml is unreadable; nothing is recorded"
  for lane in $lanes; do
    "$KIT/bin/provision-lane.sh" "$host" "$lane" --apply || failed+="provision-lane.sh:$lane "
  done
  "$KIT/bin/provision-dashboards.sh" "$host" --apply || failed+="provision-dashboards.sh "
  [ -z "$failed" ] || die "--apply failed for ${failed% }; nothing is recorded (the next tick retries)"
  install -d -m 0755 "$STATE_DIR"
  printf '%s\n' "$heads" > "$APPLIED.tmp"
  mv -f "$APPLIED.tmp" "$APPLIED"
  log "applied engine ${engine_head:0:12} and values ${config_head:0:12} to $host"
}

# Called from the last line: bash reads a script as it runs, and the fast-forward above may
# replace this file.
main "$@"
