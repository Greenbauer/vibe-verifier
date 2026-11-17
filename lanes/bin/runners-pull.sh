#!/usr/bin/env bash
# runners-pull.sh: keep this machine's engine checkout on the catalog commit its host file pins, keep
# its values checkout on origin/main, and converge it.
#
# A machine has two checkouts. The engine is a clone of the catalog, whose lanes/ directory is this
# kit; it is public and fetched with no credential. The values checkout is the owner's private
# repository, whose root holds hosts/<host>.yml; it is fetched with the machine's read-only deploy
# key. The host file's `engine` names the full catalog commit the machine runs as root, so a merge to
# the public catalog runs nowhere until a commit to the private values repository moves that pin
# (README.md, "Deploy model").
#
# runners-pull.service runs this as root every 5 minutes (runners-pull.timer, written by
# bin/provision-host.sh). One run:
#   1. takes a lock, so a run that outlives its tick never overlaps the next;
#   2. refuses, logging why and exiting non-zero, when either checkout has local modifications (a
#      hand edit on the machine; change the repository instead);
#   3. fetches the values checkout's origin with the deploy key and fast-forwards it to origin/main;
#   4. reads the pin, `engine` in hosts/<host>.yml, with PyYAML alone rather than the kit's loader,
#      so an engine on any earlier commit can read a host file written for a later one;
#   5. when the engine is not at the pin: fetches the catalog's origin, refuses a pin that is not on
#      the catalog's main (a fork's commit is fetchable by id) or, while /etc/runners/engine_signers
#      exists, one no engine/* tag signed by a key listed there points at; then checks the pin out
#      (detached, so a pin may move back as well as forward). An engine at its pin fetches nothing.
#      A host file with no pin follows origin/main as before, with a warning, while machines move;
#   6. when that move changed this script, runs the new copy in its place, so a change to what a run
#      applies takes effect on the tick that moved to it;
#   7. when the kit or the values moved, or no successful apply is recorded for the pair, runs
#      bin/provision-host.sh <host> --apply, then bin/provision-lane.sh <host> <lane> --apply for
#      each lane of hosts/<host>.yml, then bin/provision-dashboards.sh <host> --apply, <host> being
#      the one line of /etc/runners-host. The kit is the tree of lanes/ in the engine checkout, not
#      the engine's commit: a pin moved over catalog commits that touch nothing under lanes/
#      applies only because the values commit moved;
#   8. records the pair as applied only when every one of them succeeded, so a failure is retried
#      at the next tick. A failed host apply skips the rest; a failed lane stops neither the other
#      lanes nor the dashboards.
#
# Linux-only by design: Ubuntu 24.04, bash 5, git, util-linux flock, OpenSSH, python3 with PyYAML.
set -euo pipefail

ENGINE="${RUNNERS_ENGINE:-/opt/runner-lanes}"
CONFIG="${RUNNERS_CONFIG:-/opt/runner-lanes-config}"
# This kit inside the engine checkout, and this script's path inside that repository.
KIT="$ENGINE/lanes"
SELF="lanes/bin/runners-pull.sh"
HOST_FILE="${RUNNERS_HOST_FILE:-/etc/runners-host}"
DEPLOY_KEY="${RUNNERS_DEPLOY_KEY:-/etc/runners/deploy_key}"
# The operator's allowed-signers file (ssh-keygen ALLOWED SIGNERS format). While it exists, the engine
# moves only to a commit an engine/* tag signed by one of its keys points at.
SIGNERS="${RUNNERS_ENGINE_SIGNERS:-/etc/runners/engine_signers}"
STATE_DIR="${RUNNERS_PULL_STATE_DIR:-/var/lib/runners}"
LOCK="${RUNNERS_PULL_LOCK:-/run/runners-pull.lock}"
APPLIED="$STATE_DIR/applied"

log() { printf 'runners-pull: %s\n' "$*"; }
die() { printf 'runners-pull: %s\n' "$*" >&2; exit 1; }

# The host file's `engine`, or nothing when it has none. PyYAML alone: the kit's loader at the engine's
# current commit may refuse a host file written for the commit it pins.
engine_pin() {
  python3 -c 'import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
value = doc.get("engine") if isinstance(doc, dict) else None
print("" if value is None else value)' "$1"
}

# Exits 0 when an engine/* tag pointing at $1 carries a signature from a key in $SIGNERS.
signed_by_operator() {
  local tag
  git -C "$ENGINE" fetch --quiet --no-tags origin '+refs/tags/engine/*:refs/tags/engine/*' || return 1
  for tag in $(git -C "$ENGINE" tag --list 'engine/*' --points-at "$1"); do
    git -C "$ENGINE" -c gpg.ssh.allowedSignersFile="$SIGNERS" verify-tag "$tag" >/dev/null 2>&1 && return 0
  done
  return 1
}

# Moves the engine checkout to the pinned commit $1, verified first.
move_engine() {
  local pin="$1"
  [ "$(git -C "$ENGINE" rev-parse HEAD)" != "$pin" ] || return 0
  git -C "$ENGINE" fetch --quiet origin || die "git fetch failed in $ENGINE"
  git -C "$ENGINE" cat-file -e "$pin^{commit}" 2>/dev/null \
    || die "the pinned engine $pin is not a commit of the catalog; the engine stays at $(git -C "$ENGINE" rev-parse --short=12 HEAD) and nothing is applied"
  git -C "$ENGINE" merge-base --is-ancestor "$pin" origin/main \
    || die "the pinned engine $pin is not on the catalog's main; the engine stays at $(git -C "$ENGINE" rev-parse --short=12 HEAD) and nothing is applied"
  if [ -e "$SIGNERS" ] && ! signed_by_operator "$pin"; then
    die "no engine/* tag signed by a key in $SIGNERS points at the pinned engine $pin; the engine stays at $(git -C "$ENGINE" rev-parse --short=12 HEAD) and nothing is applied (README.md, Deploy model)"
  fi
  git -C "$ENGINE" -c advice.detachedHead=false checkout --quiet --detach "$pin" || die "$ENGINE cannot check out $pin"
  log "engine moved to the pinned ${pin:0:12}"
}

main() {
  local host checkout engine_before engine_head kit_before kit_tree config_before config_head record host_yml pin lanes lane failed=""
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
  kit_before="$(git -C "$ENGINE" rev-parse HEAD:lanes)"
  config_before="$(git -C "$CONFIG" rev-parse HEAD)"
  # The values first: their host file pins the engine.
  GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
    git -C "$CONFIG" fetch --quiet origin || die "git fetch failed in $CONFIG"
  git -C "$CONFIG" merge --ff-only --quiet origin/main || die "$CONFIG cannot fast-forward to origin/main"
  config_head="$(git -C "$CONFIG" rev-parse HEAD)"
  host_yml="${RUNNERS_HOST_YML:-$CONFIG/hosts/$host.yml}"
  pin="$(engine_pin "$host_yml")" || die "$host_yml is unreadable; nothing is applied"
  if [ -n "$pin" ]; then
    [[ "$pin" =~ ^[0-9a-f]{40}$ ]] || die "engine in $host_yml must be a full 40-character lowercase commit id, got '$pin'; nothing is applied"
    move_engine "$pin"
  else
    # The engine is public: no key is offered for it.
    log "$host_yml pins no engine commit; following the catalog's main until it does (README.md, Deploy model)"
    git -C "$ENGINE" fetch --quiet origin || die "git fetch failed in $ENGINE"
    git -C "$ENGINE" merge --ff-only --quiet origin/main || die "$ENGINE cannot fast-forward to origin/main"
  fi
  engine_head="$(git -C "$ENGINE" rev-parse HEAD)"
  if [ "$engine_head" != "$engine_before" ] && ! git -C "$ENGINE" diff --quiet "$engine_before" "$engine_head" -- "$SELF"; then
    # This run's own steps are the old copy's; the new copy sees this kit unapplied and applies it.
    log "$SELF changed in ${engine_before:0:12}..${engine_head:0:12}; running the new copy"
    exec "$KIT/bin/runners-pull.sh"
  fi
  # What an apply depends on: the kit's tree, lanes/ in the engine checkout, and the values commit.
  # An engine commit that leaves lanes/ as it was has the same tree id, so it applies nothing.
  kit_tree="$(git -C "$ENGINE" rev-parse HEAD:lanes)"
  # bin/provision-host.sh --check reads the record in this form.
  record="engine=$kit_tree config=$config_head"
  if [ "$kit_tree" = "$kit_before" ] && [ "$config_head" = "$config_before" ] && [ "$(cat "$APPLIED" 2>/dev/null || true)" = "$record" ]; then
    return 0
  fi
  # The engine's commit range, so a journal reader can find the merge that changed the kit.
  log "engine ${engine_before:0:12} -> ${engine_head:0:12} (kit tree ${kit_before:0:12} -> ${kit_tree:0:12}), values ${config_before:0:12} -> ${config_head:0:12}; converging $host"
  if ! "$KIT/bin/provision-host.sh" "$host" --apply; then
    die "provision-host.sh $host --apply failed; the lanes were not applied and nothing is recorded (the next tick retries)"
  fi
  lanes="$(python3 "$KIT/lib/lanes.py" lanes "$host_yml")" || die "$host_yml is unreadable; nothing is recorded"
  for lane in $lanes; do
    "$KIT/bin/provision-lane.sh" "$host" "$lane" --apply || failed+="provision-lane.sh:$lane "
  done
  "$KIT/bin/provision-dashboards.sh" "$host" --apply || failed+="provision-dashboards.sh "
  [ -z "$failed" ] || die "--apply failed for ${failed% }; nothing is recorded (the next tick retries)"
  install -d -m 0755 "$STATE_DIR"
  printf '%s\n' "$record" > "$APPLIED.tmp"
  mv -f "$APPLIED.tmp" "$APPLIED"
  log "applied engine ${engine_head:0:12} (kit tree ${kit_tree:0:12}) and values ${config_head:0:12} to $host"
}

# Called from the last line: bash reads a script as it runs, and the fast-forward above may
# replace this file.
main "$@"
