#!/usr/bin/env python3
"""Would a PR-size gate have flagged the PRs that later needed a fix? Read-only.

For each threshold, counts the feature and fix PRs it flags, how many of those a later fix rewrote
(pr_outcomes.py's `fixed_by`), and how many of the later-fixed PRs it caught. Compare the flagged
share that was later fixed with the base rate: a useful threshold is well above it.

usage: size_backtest.py --outcomes OUTCOMES.json [OUTCOMES.json ...]
"""
import argparse
import json

THRESHOLDS = [("lines", 100), ("lines", 200), ("lines", 400), ("files", 3), ("files", 5), ("files", 8)]


def size(pr, measure):
    return pr["additions"] + pr["deletions"] if measure == "lines" else pr["files"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outcomes", nargs="+", required=True)
    args = parser.parse_args()
    prs = [pr for path in args.outcomes for pr in json.load(open(path))["prs"] if pr["kind"] in ("change", "fix")]
    fixed = [pr for pr in prs if pr["fixed_by"]]
    print(f"n={len(prs)} later fixed={len(fixed)} base rate={len(fixed) / len(prs):.2f}" if prs else "no PRs")
    for measure, limit in THRESHOLDS:
        flagged = [pr for pr in prs if size(pr, measure) > limit]
        hits = [pr for pr in flagged if pr["fixed_by"]]
        share = f"{len(hits) / len(flagged):.2f}" if flagged else "-"
        print(f"{measure} > {limit:<4} flagged={len(flagged):<4} later fixed among flagged={len(hits):<4} "
              f"({share})  caught {len(hits)} of {len(fixed)}")


if __name__ == "__main__":
    main()
