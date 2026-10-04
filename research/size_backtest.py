#!/usr/bin/env python3
"""Exploratory PR size versus later-fix touches. No causal or gate-quality verdict.

For each threshold, counts the feature and fix PRs it flags, how many of those a later fix rewrote
(pr_outcomes.py's `fixed_by`), and how many of the later-fixed PRs it caught. Compare the flagged
share that was later fixed with the base rate: this is association, not evidence of a useful gate.

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
    prs = [pr for path in args.outcomes for pr in json.load(open(path))["prs"]
           if pr["kind"] in ("change", "fix") and pr["has_full_window"]]
    fixed = [pr for pr in prs if pr["fixed_by"]]
    print(f"n={len(prs)} later-fix-touched={len(fixed)} touch share={len(fixed) / len(prs):.2f}" if prs else "no PRs")
    for measure, limit in THRESHOLDS:
        flagged = [pr for pr in prs if size(pr, measure) > limit]
        hits = [pr for pr in flagged if pr["fixed_by"]]
        share = f"{len(hits) / len(flagged):.2f}" if flagged else "-"
        print(f"{measure} > {limit:<4} flagged={len(flagged):<4} linked among flagged={len(hits):<4} "
              f"({share})  selected {len(hits)} of {len(fixed)} linked sources")


if __name__ == "__main__":
    main()
