#!/usr/bin/env bash
# run-all.sh: run every hermetic test in tests/ and report the totals. The listener's Go tests are
# its own (listener/build.sh test).
#
# Linux with GNU userland: Ubuntu 24.04 is the target. Needs bash 5, python3 with PyYAML, jq, git
# and util-linux's flock; never root, a network, Docker or a GitHub login.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
export PYTHONDONTWRITEBYTECODE=1

checks_passed=0
checks_failed=0
files=0
failed_files=()
for test in tests/*-test.sh tests/test_*.py; do
  files=$((files + 1))
  echo "=== $test"
  case "$test" in
    *.sh) out="$(bash "$test" 2>&1)"; rc=$? ;;
    *.py) out="$(python3 "$test" 2>&1)"; rc=$? ;;
  esac
  printf '%s\n' "$out"
  checks_passed=$(( checks_passed + $(grep -c '^✓' <<< "$out") ))
  checks_failed=$(( checks_failed + $(grep -c '^✗' <<< "$out") ))
  [ "$rc" -eq 0 ] || failed_files+=("$test")
done

echo
echo "run-all: $checks_passed checks passed, $checks_failed failed, in $files test files"
if [ "${#failed_files[@]}" -gt 0 ]; then
  echo "run-all: FAILED: ${failed_files[*]}"
  exit 1
fi
