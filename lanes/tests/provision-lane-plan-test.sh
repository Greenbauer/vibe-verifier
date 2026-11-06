#!/bin/bash
# provision-lane-plan-test.sh: pins everything bin/provision-lane.sh prints, runs and writes, run by
# run, against the recorded copies in tests/fixtures/provision-lane/. Each run is one mode on one
# lane in the hermetic setup provision-lane-test.sh uses (tests/lib/provision-lane-harness.sh): its
# exit code, its output, every stubbed command it ran, and the files it left in the lane's unit, etc,
# lib, state and run directories. The temporary paths and the clock are replaced by placeholders, so
# a difference is a change of behaviour: a message, a command, its order, or a file's content.
#
# A deliberate change records the new copies with UPDATE_PLAN_FIXTURES=1, and the diff of the
# fixtures shows what changed.
set -u
# The modes of the files a run leaves are pinned, so they must not depend on the caller's umask: 022,
# root's, as on the machines.
umask 022
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/provision-lane-harness.sh
. "$ROOT/tests/lib/provision-lane-harness.sh"
FIXTURES="$ROOT/tests/fixtures/provision-lane"

# The temporary directories, the clock and the durations, as placeholders: replaced as literal text,
# the longest first ($S lies under $TMP), and the store's mount unit in the escaped form systemd
# names it by.
normalize() {
  local text unit
  text="$(cat)"
  unit="$(printf '%s' "${S#/}" | sed 's/-/\\x2d/g; s#/#-#g')"
  text="${text//"$unit"/@SUNIT}"; text="${text//"$S"/@S}"; text="${text//"$ROOT"/@ROOT}"; text="${text//"$TMP"/@TMP}"
  sed -e 's#[0-9]\{8\}T[0-9]\{6\}Z#@STAMP#g' -e 's#[0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9:.]*Z#@TIME#g' \
    -e 's#\(_s=\)-\{0,1\}[0-9][0-9]*#\1@N#g' -e 's#smoke-[0-9][0-9]*#smoke-@PID#g' -e 's#\(container-ready ok \)[0-9]*#\1@EPOCH#' <<< "$text" | sort_copies
}

# The reflink copies of a store's layers run in the order find lists the directory, which is the
# filesystem's: each run of them is sorted.
sort_copies() {
  local line run=()
  while IFS= read -r line; do
    if [[ "$line" == "cp --reflink=always -a "*/overlay2/./* ]]; then run+=("$line"); continue; fi
    [ "${#run[@]}" -eq 0 ] || { printf '%s\n' "${run[@]}" | LC_ALL=C sort; run=(); }
    printf '%s\n' "$line"
  done
  [ "${#run[@]}" -eq 0 ] || printf '%s\n' "${run[@]}" | LC_ALL=C sort
}

# Every file under the lane's directories: its path and mode, and with $1 = contents the content of
# each unit, config and listener file. A .cache directory is skipped: the script writes none, and
# Rosetta (an amd64 container on an arm64 Mac) makes one under the lane user's home.
files_left() {
  local dir f
  for dir in units etc lib state run healthrun; do
    [ -d "$S/$dir" ] || continue
    while IFS= read -r f; do
      if [ -d "$f" ]; then
        echo "dir $(mode_of "$f") $f"
      elif [ -f "$f" ]; then
        echo "file $(mode_of "$f") $f"
        case "$1:$dir" in contents:units|contents:etc|contents:lib) sed 's/^/  | /' "$f" ;; esac
      else
        echo "other $f"
      fi
    done < <(find "$S/$dir" -mindepth 1 -name .cache -prune -o -print | LC_ALL=C sort)
  done
}

# plan <name> <runner> <args...>: run it, then compare what it did with fixtures/<name>.txt.
plan() {
  local name="$1" got rc contents=list
  shift
  # A run that writes (an --apply that is no --remove) has its files' content pinned too.
  case " $* " in *" --remove "*) ;; *" --apply "*) contents=contents ;; esac
  : > "$CALLLOG"
  got="$("$@" 2>&1)"; rc=$?
  got="$(printf 'exit %s\n--- output\n%s\n--- commands\n%s\n--- files\n%s\n' "$rc" "$got" "$(cat "$CALLLOG")" "$(files_left "$contents")" | normalize)"
  if [ "${UPDATE_PLAN_FIXTURES:-0}" = 1 ]; then
    mkdir -p "$FIXTURES"
    printf '%s\n' "$got" > "$FIXTURES/$name.txt"
  fi
  diff -u "$FIXTURES/$name.txt" - <<< "$got" > "$TMP/diff" 2>&1
  expect $? "$name: the output, commands and files are the recorded ones (fixtures/provision-lane/$name.txt)"
  [ -s "$TMP/diff" ] && head -n 60 "$TMP/diff"
  return 0
}
run_convert() { RUNNERS_LANE_BUILD_LOCK="$S/build.lock" run_org --convert-store "$@"; }
run_wait() { HOST_YML_UNDER_TEST="$ROOT/tests/fixtures/hosts/vps-1.yml" run_lane orbit-ci "$@"; }

# The command line: usage, and the combinations it refuses.
setup
plan usage run_org --help
plan no-lane run_lane ""
plan check-with-apply run_org --check --apply
plan remove-with-convert run_org --remove --convert-store
plan unknown-flag run_org --force

# An org lane with QAE: dry run, then the first and the second --apply, --check, --remove.
setup
plan org-dry-run run_org
plan org-apply run_org --apply
plan org-apply-again run_org --apply
converged_org
plan org-check run_org --check
plan org-remove-dry-run run_org --remove
plan org-remove run_org --remove --apply

# A user lane, with and without its App key.
setup
plan user-dry-run run_user
plan user-apply-no-key run_user --apply
setup
install -d -m 0700 "$S/etc/own-ci"; printf '%s\n' "$KEY_MARKER" > "$S/etc/own-ci/app.pem"; chmod 0600 "$S/etc/own-ci/app.pem"
plan user-apply run_user --apply
mkdir -p "$GHDIR"; printf 'github.com:\n    oauth_token: %s\n' "$APP_SESSION_MARKER" > "$GHDIR/hosts.yml"
login_json > "$S/state/codex/auth.json"
smoke_out "22 3101 54329" > "$S/fx/smoke.out"
plan user-check run_user --check

# The wait kind (vps-1's orbit-ci): its own template, budget and listener kind.
setup
plan wait-dry-run run_wait
install -d -m 0700 "$S/etc/orbit-ci"; printf '%s\n' "$KEY_MARKER" > "$S/etc/orbit-ci/app.pem"; chmod 0600 "$S/etc/orbit-ci/app.pem"
plan wait-apply run_wait --apply

# The store: a btrfs lane, and the conversion of an XFS store, planned and made.
mounted_org btrfs
plan btrfs-check run_org --check
conversion_fixture
plan convert-dry-run run_convert
unset IMAGES_ABSENT

[ "$fail" -eq 0 ]; expect $? "every recorded run is unchanged: exit code, output, stubbed commands and files left"

echo "provision-lane-plan-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
