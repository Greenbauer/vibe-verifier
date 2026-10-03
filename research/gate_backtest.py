#!/usr/bin/env python3
"""Replay merged pull requests through today's gates and score them against later fixes.

For each merged PR, check out its merge commit in a scratch worktree, run every gate on the
manifest with the merge commit's first parent as the base, and record the status. Joined with
pr_outcomes.py's `fixed_by`, each gate gets a confusion table: did it fire on the PRs whose lines
a later fix changed (hits), and on the ones nobody fixed (false alarms)?

usage: gate_backtest.py --repo CLONE --outcomes OUTCOMES.json --manifest FILE [--format text|json]

OUTCOMES.json is pr_outcomes.py --format json. The manifest uses the .vibe-verifier format.
"""
import argparse
import json
import pathlib
import shlex
import subprocess
import tempfile

GATES = pathlib.Path(__file__).resolve().parent.parent / "gates"


def manifest_lines(path):
    for raw in open(path):
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if line:
            name, *args = shlex.split(line)
            yield line, name, [a for a in args if a != "--soak"]


def run_gate(name, args, tree, base):
    script = GATES / (name.replace("-", "_") + ".py")
    cmd = ["python3", str(script), "--repo", tree, "--base-ref", base, "--format", "json", *args]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return {0: "pass", 1: "violations"}.get(done.returncode, "cannot-run")


def replay(repo, prs, gates):
    results = {}
    with tempfile.TemporaryDirectory() as scratch:
        for pr in prs:
            tree = f"{scratch}/pr-{pr['number']}"
            subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", "-q", tree, pr["merge_commit"]], check=True)
            try:
                results[pr["number"]] = {line: run_gate(name, args, tree, pr["merge_commit"] + "^1") for line, name, args in gates}
            finally:
                subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", tree], check=True)
    return results


def confusion(prs, results, gate):
    table = {"hit": 0, "miss": 0, "false_alarm": 0, "quiet": 0, "cannot_run": 0}
    for pr in prs:
        status = results[pr["number"]][gate]
        escaped = bool(pr["fixed_by"])
        if status == "cannot-run":
            table["cannot_run"] += 1
        elif status == "violations":
            table["hit" if escaped else "false_alarm"] += 1
        else:
            table["miss" if escaped else "quiet"] += 1
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--outcomes", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    prs = [pr for pr in json.load(open(args.outcomes))["prs"] if pr["kind"] in ("change", "fix")]
    gates = list(manifest_lines(args.manifest))
    results = replay(args.repo, prs, gates)
    report = {line: confusion(prs, results, line) for line, _, _ in gates}
    if args.format == "json":
        print(json.dumps({"per_pr": results, "per_gate": report}, indent=2))
        return
    for pr in prs:
        fired = [name for name, status in results[pr["number"]].items() if status != "pass"]
        mark = "ESCAPED" if pr["fixed_by"] else "clean  "
        print(f"#{pr['number']:<4} {mark} {', '.join(f'{n}={results[pr['number']][n]}' for n in fired) or '-'}")
    for name, table in report.items():
        print(f"{name:<34} {table}")


if __name__ == "__main__":
    main()
