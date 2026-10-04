#!/usr/bin/env python3
"""Replay merged pull requests through today's gates and score them against later fixes.

For each merged PR, check out its merge commit in a scratch worktree, run every gate on the
manifest with the merge commit's first parent as the base, and record the status. Joined with
pr_outcomes.py's `fixed_by`, each gate gets an association table. A violation on a PR with no
later-fix link is not a false alarm: the gate may catch a real security or hygiene problem.
Only complete follow-up windows enter this comparison. Keep diagnostics for unrun gates.

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
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"status": "cannot-run", "returncode": None, "stdout": "", "stderr": "gate timed out after 600s"}
    return {"status": {0: "pass", 1: "violations"}.get(done.returncode, "cannot-run"),
            "returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


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
    table = {"flagged_with_link": 0, "passed_with_link": 0,
             "flagged_without_link": 0, "passed_without_link": 0, "cannot_run": 0}
    for pr in prs:
        status = results[pr["number"]][gate]["status"]
        escaped = bool(pr["fixed_by"])
        if status == "cannot-run":
            table["cannot_run"] += 1
        elif status == "violations":
            table["flagged_with_link" if escaped else "flagged_without_link"] += 1
        else:
            table["passed_with_link" if escaped else "passed_without_link"] += 1
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--outcomes", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    prs = [pr for pr in json.load(open(args.outcomes))["prs"]
           if pr["kind"] in ("change", "fix") and pr["has_full_window"]]
    gates = list(manifest_lines(args.manifest))
    results = replay(args.repo, prs, gates)
    report = {line: confusion(prs, results, line) for line, _, _ in gates}
    if args.format == "json":
        print(json.dumps({"per_pr": results, "per_gate": report}, indent=2))
        return
    for pr in prs:
        fired = [name for name, result in results[pr["number"]].items() if result["status"] != "pass"]
        mark = "LINKED " if pr["fixed_by"] else "no link"
        statuses = ", ".join(n + "=" + results[pr["number"]][n]["status"] for n in fired)
        print(f"#{pr['number']:<4} {mark} {statuses or '-'}")
    for name, table in report.items():
        print(f"{name:<34} {table}")


if __name__ == "__main__":
    main()
