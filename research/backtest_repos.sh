#!/usr/bin/env bash
# Run every historical analysis in this directory against one or more repositories.
#
# usage: research/backtest_repos.sh OUT_DIR OWNER/NAME[=MANIFEST] [OWNER/NAME[=MANIFEST] ...]
#
# Read-only: it clones or fetches each repository and reads PR metadata through `gh api`; it never
# pushes, comments, or touches any database. Results go to OUT_DIR/<owner>__<name>/, which must be
# outside this public repository so a private repository's history never lands in it.
# MANIFEST is the gate list to replay (a wrapper's entry for that repository, say); without it the
# repository's own .vibe-verifier is used, else the catalog's diff-scoped gates.
# SINCE=YYYY-MM-DD limits the analysis to PRs merged on or after that date.
# OBSERVED_AT is the metadata snapshot timestamp; only mature PRs enter comparisons.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
catalog="$(cd "$here/.." && pwd)"
out="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${1:?usage: $0 OUT_DIR OWNER/NAME[=MANIFEST] ...}")"
shift
[ "$#" -gt 0 ] || { echo "name at least one OWNER/NAME" >&2; exit 2; }
case "$out/" in
  "$catalog"/*) echo "OUT_DIR is inside $catalog; choose a directory outside this public repository" >&2; exit 2 ;;
esac
mkdir -p "$out"

default_manifest="$out/catalog-diff-gates"
printf '%s\n' gitleaks actionlint zizmor cognitive-complexity max-file-lines new-source-has-test \
  no-duplicate-package-json-keys > "$default_manifest"

for spec in "$@"; do
  repo="${spec%%=*}"
  manifest=""
  [ "$spec" != "$repo" ] && manifest="${spec#*=}"
  dir="$out/${repo//\//__}"
  mkdir -p "$dir"
  if [ -d "$dir/clone/.git" ]; then
    git -C "$dir/clone" fetch -q origin
  else
    gh repo clone "$repo" "$dir/clone" -- -q
  fi
  default_branch="$(gh api "repos/$repo" --jq .default_branch)"
  git -C "$dir/clone" checkout -q --detach "origin/$default_branch"
  if [ -z "$manifest" ]; then
    manifest="$dir/clone/.vibe-verifier"
    [ -f "$manifest" ] || manifest="$default_manifest"
  fi
  # A replayed merge commit has no branch, so branch-name-length can never run on history.
  grep -v '^[[:space:]]*branch-name-length' "$manifest" > "$dir/replayed-gates" || true
  manifest="$dir/replayed-gates"

  echo "== $repo (manifest: $manifest)"
  (cd "$dir/clone" && "$here/fetch_prs.sh" "${repo%%/*}" "${repo#*/}") > "$dir/prs.jsonl"
  observed_at="${OBSERVED_AT:-$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')}"
  python3 "$here/pr_outcomes.py" --repo "$dir/clone" --prs "$dir/prs.jsonl" --base-branch "$default_branch" --since "${SINCE:-}" --observed-at "$observed_at" --format json > "$dir/outcomes.json"
  python3 "$here/gate_backtest.py" --repo "$dir/clone" --outcomes "$dir/outcomes.json" --manifest "$manifest" --format json > "$dir/gates.json"
  python3 "$here/test_oracles.py" --repo "$dir/clone" --outcomes "$dir/outcomes.json" --labels "$dir/oracle-labels.csv" --format json > "$dir/oracles.json"
  {
    echo "# $repo, PRs merged since ${SINCE:-the start}"
    python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['summary'], indent=2))" "$dir/outcomes.json"
    echo "## PR size as a predictor"
    python3 "$here/size_backtest.py" --outcomes "$dir/outcomes.json"
    echo "## Test cases each PR touched"
    python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['summary'], indent=2))" "$dir/oracles.json"
    echo "## Today's gates replayed"
    python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['per_gate'], indent=2))" "$dir/gates.json"
  } | tee "$dir/summary.txt"
done
