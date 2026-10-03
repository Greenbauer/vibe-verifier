#!/usr/bin/env python3
"""Outcome metrics for merged pull requests, from git history and saved PR metadata.

A fix PR is linked to the PRs that introduced the lines it changed (SZZ: blame each deleted or
modified line at the fix's parent). A PR whose lines a later fix changed within the window is an
escape. Pure additions in a fix cannot be blamed and are counted as unattributed.

usage: pr_outcomes.py --repo CLONE --prs PRS.jsonl [--window-days 14] [--format text|json]

PRS.jsonl is one GitHub GraphQL pullRequest node per line (research/fetch_prs.sh writes it).
"""
import argparse
import json
import re
import statistics
import subprocess
from datetime import datetime, timedelta

FIX_TITLE = re.compile(r"^(fix|revert)\b", re.IGNORECASE)
CONVENTIONAL = re.compile(r"^(feat|fix|revert|docs|chore|ci|refactor|perf|test|build|style)(\(.*?\))?!?:")
IGNORED_FILES = re.compile(r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lock)$")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def kind_of(pr):
    title, branch = pr["title"], pr["headRefName"]
    if pr["author"] and pr["author"]["login"].startswith("dependabot") or title.startswith("Bump "):
        return "deps"
    if FIX_TITLE.match(title) or not CONVENTIONAL.match(title) and branch.startswith(("fix/", "hotfix/")):
        return "fix"
    if title.startswith("docs") or all(f["path"].endswith(".md") for f in pr["files"]["nodes"]):
        return "docs"
    return "change"


def changed_old_lines(repo, parent, commit):
    """(path, first, count) for every hunk of `commit` that removes or rewrites lines of `parent`."""
    hunks, unattributed, path = [], 0, None
    for line in git(repo, "diff", "-U0", "--no-renames", parent, commit).splitlines():
        if line.startswith("--- "):
            path = None if line == "--- /dev/null" else line[6:]
        match = HUNK.match(line)
        if not match or path is None or IGNORED_FILES.search(path):
            continue
        first, count = int(match.group(1)), int(match.group(2) or 1)
        if count == 0:
            unattributed += 1
        else:
            hunks.append((path, first, count))
    return hunks, unattributed


def blamed_commits(repo, parent, hunks):
    shas = []
    for path, first, count in hunks:
        out = git(repo, "blame", "--porcelain", "-w", "-L", f"{first},+{count}", parent, "--", path)
        sha = None
        for line in out.splitlines():
            if re.match(r"^[0-9a-f]{40} ", line):
                sha = line.split()[0]
            elif line.startswith("\t") and line.strip():
                shas.append(sha)
    return shas


def parse_time(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def outcomes(repo, prs, window_days):
    merged = [pr for pr in prs if pr["mergedAt"] and pr["mergeCommit"]]
    owner = {}
    for pr in merged:
        for node in pr["prCommits"]["nodes"]:
            owner[node["commit"]["oid"]] = pr["number"]
        owner[pr["mergeCommit"]["oid"]] = pr["number"]
    by_number = {pr["number"]: pr for pr in merged}
    fixed_by = {pr["number"]: [] for pr in merged}
    unattributed = {}
    for pr in merged:
        if kind_of(pr) != "fix":
            continue
        commit = pr["mergeCommit"]["oid"]
        hunks, unattributed[pr["number"]] = changed_old_lines(repo, commit + "^1", commit)
        window_start = parse_time(pr["mergedAt"]) - timedelta(days=window_days)
        for introducing in set(owner.get(sha) for sha in blamed_commits(repo, commit + "^1", hunks)):
            source = by_number.get(introducing)
            if source and source is not pr and parse_time(source["mergedAt"]) >= window_start:
                fixed_by[introducing].append(pr["number"])
    rows = []
    for pr in merged:
        hours = (parse_time(pr["mergedAt"]) - parse_time(pr["createdAt"])).total_seconds() / 3600
        rows.append({
            "number": pr["number"], "kind": kind_of(pr), "title": pr["title"],
            "merged_at": pr["mergedAt"], "merge_commit": pr["mergeCommit"]["oid"],
            "additions": pr["additions"], "deletions": pr["deletions"],
            "files": pr["changedFiles"], "pushes": pr["commits"]["totalCount"],
            "review_threads": pr["reviewThreads"]["totalCount"], "lead_time_hours": round(hours, 2),
            "fixed_by": sorted(set(fixed_by[pr["number"]])),
            "unattributed_fix_hunks": unattributed.get(pr["number"], 0),
        })
    return rows


def summary(rows):
    candidates = [r for r in rows if r["kind"] in ("change", "fix")]
    escaped = [r for r in candidates if r["fixed_by"]]
    fixes = [r for r in rows if r["kind"] == "fix"]
    linked = [r for r in fixes if any(r["number"] in o["fixed_by"] for o in rows)]

    def median(values):
        return statistics.median(values) if values else None

    return {
        "merged": len(rows),
        "by_kind": {k: sum(r["kind"] == k for r in rows) for k in ("change", "fix", "docs", "deps")},
        "rework_share": round(len(fixes) / len(rows), 3) if rows else None,
        "escape_rate": round(len(escaped) / len(candidates), 3) if candidates else None,
        "fixes_linked_to_a_recent_pr": f"{len(linked)}/{len(fixes)}",
        "median_lines_escaped": median([r["additions"] + r["deletions"] for r in escaped]),
        "median_lines_clean": median([r["additions"] + r["deletions"] for r in candidates if not r["fixed_by"]]),
        "median_lead_time_hours": median([r["lead_time_hours"] for r in rows]),
        "median_pushes": median([r["pushes"] for r in rows]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--prs", required=True)
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--since", default="", help="only PRs merged on or after this ISO date")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    prs = [json.loads(line) for line in open(args.prs) if line.strip()]
    rows = [r for r in outcomes(args.repo, prs, args.window_days) if r["merged_at"] >= args.since]
    if args.format == "json":
        print(json.dumps({"summary": summary(rows), "prs": rows}, indent=2))
        return
    for r in rows:
        escaped = ",".join(f"#{n}" for n in r["fixed_by"]) or "-"
        print(f"#{r['number']:<4} {r['kind']:<6} +{r['additions']}/-{r['deletions']:<6} "
              f"files={r['files']:<3} pushes={r['pushes']:<2} threads={r['review_threads']:<2} "
              f"fixed_by={escaped:<10} {r['title'][:60]}")
    print(json.dumps(summary(rows), indent=2))


if __name__ == "__main__":
    main()
