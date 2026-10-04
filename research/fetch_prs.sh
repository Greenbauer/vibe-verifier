#!/usr/bin/env bash
# usage: fetch_prs.sh OWNER NAME > prs.jsonl
set -euo pipefail
exec python3 "$(dirname "$0")/fetch_prs.py" "$@"
