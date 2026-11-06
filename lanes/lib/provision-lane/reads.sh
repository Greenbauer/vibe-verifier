# shellcheck shell=bash
# reads.sh: what the lane's phases read: GitHub, through the lane's own App session, and the machine.
# Sourced by bin/provision-lane.sh after it has read the lane's settings and set its paths; never run.

# The lane's App installation session, as gh reads it: the lane user's own hosts.yml (the A3
# timer). gh runs as that user, so it writes nothing root-owned into that user's config.
# GH_CONFIG_DIR is always set, and never an ambient GH_TOKEN or GITHUB_TOKEN: either would
# silently swap the identity.
gh_lane() {
  runuser -u "$LANE_USER" -- env -u GH_TOKEN -u GITHUB_TOKEN HOME="$LANE_HOME" GH_CONFIG_DIR="$GH_CONFIG" GH_PROMPT_DISABLED=1 gh "$@"
}

# gh run as root writes a missing config.yml into the session's directory as root:root 0600, and
# the directory's own user can then not read its config: gh as the lane's user failed with
# "failed to read configuration: ... permission denied" on its first bring-up (2026-09-28). Every
# root-owned entry there goes back to the directory's owner; -h never follows a link. Runs before
# the first GitHub read and at exit, and changes nothing outside --apply.
repair_gh_config() {
  local f
  [ -d "$GH_CONFIG" ] && [ "$(stat -c %u "$GH_CONFIG" 2>/dev/null)" != 0 ] || return 0
  for f in "$GH_CONFIG"/*; do
    [ "$(stat -c %u "$f" 2>/dev/null)" = 0 ] || continue
    log "  repair: $f is root's; it goes back to the owner of $GH_CONFIG"
    run_mutation chown -h --reference="$GH_CONFIG" "$f" || warn "could not hand $f back to the owner of $GH_CONFIG"
  done
}

has_qae() { [ -n "$QAE_LABEL" ]; }

lookup_group() {  # prints the group id, empty when absent; returns 1 when the API is unreadable
  gh_lane api "orgs/$OWNER/actions/runner-groups" --paginate \
    --jq ".runner_groups[] | select(.name == \"$GROUP\") | .id" 2>/dev/null
}

group_repos() {
  gh_lane api "orgs/$OWNER/actions/runner-groups/$1/repositories" --paginate \
    --jq '.repositories[].name' 2>/dev/null | sort
}

# "<scope target> <id> <name> <status> <busy>" per lane registration: the organisation's runners,
# or every installation repository's for a user lane. Fails when any list is unreadable.
lane_runners() {
  local repos repo
  if [ "$SCOPE" = org ]; then
    gh_lane api "orgs/$OWNER/actions/runners" --paginate \
      --jq "$LANE_RUNNER_FILTER | \"$OWNER \(.id) \(.name) \(.status) \(.busy)\"" 2>/dev/null
    return
  fi
  repos="$(gh_lane api installation/repositories --paginate \
    --jq ".repositories[] | select((.owner.login | ascii_downcase) == (\"$OWNER\" | ascii_downcase)) | .full_name" 2>/dev/null)" || return 1
  for repo in $repos; do
    gh_lane api "repos/$repo/actions/runners" --paginate \
      --jq "$LANE_RUNNER_FILTER | \"$repo \(.id) \(.name) \(.status) \(.busy)\"" 2>/dev/null || return 1
  done
}

runners_endpoint() {  # the runners API of a scope target: the organisation, or owner/repo
  case "$1" in */*) printf 'repos/%s/actions/runners' "$1" ;; *) printf 'orgs/%s/actions/runners' "$1" ;; esac
}

# Runner names a slot holds right now: the listener's job files and a retiring always-on slot's
# name file.
current_slot_names() {
  { cat "$RUN_DIR"/*/name 2>/dev/null; awk '{ print $5 }' "$RUN_DIR"/*/*/job 2>/dev/null; } || true
}

image_tag() {  # empty until the lane's first image build writes image.env
  [ -r "$IMAGE_ENV" ] || return 0
  sed -n 's/^KNOWN_CI_IMAGE_TAG=//p' "$IMAGE_ENV" | tail -n 1
}

# The store's filesystem as it is on the machine, whatever the host file says: what is mounted at
# its path, else what its image holds; empty when the lane has no store yet, "unknown" for an image
# that holds nothing blkid knows.
found_store_fs() {
  local fs
  if mountpoint -q "$STORE_DIR" 2>/dev/null; then
    fs="$(findmnt -n -o FSTYPE "$STORE_DIR" 2>/dev/null || true)"
  elif [ -e "$STORE_IMG" ]; then
    fs="$(blkid -p -o value -s TYPE "$STORE_IMG" 2>/dev/null || true)"
  else
    return 0
  fi
  printf '%s\n' "${fs:-unknown}"
}

enabled_always_on_slots() {  # the retiring template's instance numbers with an enablement link
  local link
  for link in "$UNIT_DIR"/multi-user.target.wants/"$NAME"@*.service; do
    [ -e "$link" ] || [ -L "$link" ] || continue
    link="${link##*@}"
    printf '%s\n' "${link%.service}"
  done
}

always_on_slots() { { seq 1 "$SLOTS"; enabled_always_on_slots; } | sort -nu; }

# The listener's source as the build reads it: the module files and the build script, never the
# tests, so a test-only change rebuilds nothing.
listener_source_hash() {
  (
    LC_ALL=C
    cd "$LISTENER_SRC" || exit 1
    for f in build.sh go.mod go.sum *.go; do
      [ -f "$f" ] || continue
      case "$f" in *_test.go) continue ;; esac
      printf '%s %s\n' "$(hash_file "$f")" "$f"
    done
  ) | hash_stdin
}
