#!/usr/bin/env python3
"""Lexical candidate flags for changed JS/Python test cases, not test-quality judgments.

Legacy labels describe regex matches only: `none` means no assertion pattern was detected,
`weak` means only matchers on the heuristic list were detected, and `strong` means another
recognized pattern was found. Multiline/helper/SQL assertions and copied implementations can
all be misclassified. Every flag needs manual inspection. Read-only: reads git history only.

usage: test_oracles.py --repo CLONE --outcomes OUTCOMES.json [--labels FILE.csv] [--format text|json]

OUTCOMES.json is pr_outcomes.py --format json. --labels writes one row per flagged case, with an
empty `verdict` column for a person to fill in (real / helper / fine).
"""
import argparse
import csv
import json
import re
import subprocess

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|e2e|cypress)/|\.(test|spec|cy)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.py$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
JS_CASE = re.compile(r"^\s*(?:it|test)(?:\.only|\.skip|\.each\(.*?\))?\(\s*['\"`](?P<name>[^'\"`]*)")
PY_CASE = re.compile(r"^(?P<indent>\s*)(?:async\s+)?def (?P<name>test\w*)\s*\(")
JS_MATCHER = re.compile(r"\.(?:not\.)?(to\w+|should)\b")
JS_WEAK = {"toBeDefined", "toBeTruthy", "toBeFalsy", "toHaveBeenCalled", "toBeInstanceOf", "toThrow",
           "toBeUndefined", "toBeNull"}
JS_STRONG_CALLS = re.compile(r"\bassert(?:\.\w+)?\(|\.should\(|\bcy\.contains\(")
PY_ASSERT = re.compile(r"\bself\.(assert\w+|fail)\(|^\s*assert\b|\bpytest\.raises\(")
PY_WEAK = {"assertIsNotNone", "assertIsInstance"}


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def changed_lines(repo, parent, commit):
    """{path: set of line numbers in `commit`} for the test files the commit added or changed."""
    lines, path, in_hunk = {}, None, False
    for line in git(repo, "diff", "-U0", "--no-renames", parent, commit).splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = None, False
        if not in_hunk and line.startswith("+++ "):
            path = None if line == "+++ /dev/null" else line[6:]
            if path and not TEST_FILE.search(path):
                path = None
        match = HUNK.match(line)
        if match:
            in_hunk = True
        if match and path:
            first, count = int(match.group(1)), int(match.group(2) or 1)
            lines.setdefault(path, set()).update(range(first, first + count))
    return lines


def cases(source, python):
    """(name, first line, last line) for each test case, its body running to the next case or EOF."""
    pattern = PY_CASE if python else JS_CASE
    starts = [(n, m.group("name")) for n, line in enumerate(source, 1) for m in [pattern.match(line)] if m]
    return [(name, start, (starts[i + 1][0] - 1) if i + 1 < len(starts) else len(source))
            for i, (start, name) in enumerate(starts)]


def strength(body, python):
    if python:
        found = [m.group(1) or "assert" for line in body for m in PY_ASSERT.finditer(line)]
        if not found:
            return "none"
        return "weak" if all(name in PY_WEAK for name in found) else "strong"
    if any(JS_STRONG_CALLS.search(line) for line in body):
        return "strong"
    matchers = [m.group(1) for line in body if "expect" in line for m in JS_MATCHER.finditer(line)]
    if not matchers:
        return "none"
    return "weak" if all(name in JS_WEAK for name in matchers) else "strong"


def touched_cases(repo, commit, parent):
    found = []
    for path, numbers in changed_lines(repo, parent, commit).items():
        source = git(repo, "show", f"{commit}:{path}").splitlines()
        python = path.endswith(".py")
        for name, first, last in cases(source, python):
            if numbers & set(range(first, last + 1)):
                found.append({"path": path, "line": first, "name": name,
                              "strength": strength(source[first - 1:last], python)})
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--outcomes", required=True)
    parser.add_argument("--labels", default="")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    prs = [pr for pr in json.load(open(args.outcomes))["prs"] if pr["kind"] in ("change", "fix")]
    rows = [dict(pr, cases=touched_cases(args.repo, pr["merge_commit"], pr["merge_commit"] + "^1")) for pr in prs]
    all_cases = [case for row in rows for case in row["cases"]]
    summary = {
        "prs": len(rows),
        "touched_cases": len(all_cases),
        "by_strength": {k: sum(c["strength"] == k for c in all_cases) for k in ("strong", "weak", "none")},
    }
    if args.labels:
        with open(args.labels, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["pr", "path", "line", "name", "strength", "verdict"])
            for row in rows:
                for case in row["cases"]:
                    if case["strength"] != "strong":
                        writer.writerow([row["number"], case["path"], case["line"], case["name"], case["strength"], ""])
    if args.format == "json":
        print(json.dumps({"summary": summary, "prs": [{"number": r["number"], "cases": r["cases"]} for r in rows]}, indent=2))
        return
    for row in rows:
        flagged = [c for c in row["cases"] if c["strength"] != "strong"]
        if flagged:
            print(f"#{row['number']}: " + "; ".join(f"{c['strength']} {c['path']}:{c['line']} {c['name'][:50]}" for c in flagged))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
