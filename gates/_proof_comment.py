"""The proof audit's one pull request comment (actions/proof-audit, docs/comments.md): the table of this run, and
under it the earlier commits the audit judged on this pull request, each with the requirements unproven there.

    _proof_comment.py --table T --verdicts V --previous P --head SHA --run URL --out comment.json

The history is carried from the previous comment's own data block, the comment's last line, which only this
module writes: `<!-- vibe-verifier:proof-audit-history {"v": 1, "commits": [...]} -->`, newest first, one entry
per commit ({"sha", "at", "run", "verdicts": {id: status} or null when the audit graded nothing}). It never reads
the rendered table or a grader's words. A previous comment with no block, or one this module cannot read, starts
a fresh history: a comment written before the history existed is not an error. --verdicts is the gate's
`--verdicts` file ({id: status}); a run that graded nothing writes none. The step that runs this holds only the
token that comments, never the plan store's, and runs it as a script, so it imports only from the catalog.
"""
import argparse
import datetime
import json
import re
import sys

from ticket_coverage import ID

MARKER = "<!-- vibe-verifier:proof-audit -->"
BLOCK = re.compile(r"<!-- vibe-verifier:proof-audit-history (\{.*\}) -->")
# Thirty commits is ten times the three audit rounds a pull request is meant to take before a person
# decides, and thirty entries of a thirty-requirement plan stay near 30,000
# characters, half of GitHub's 65,536-character comment limit. The oldest drop first, and more drop while the
# comment would still pass LIMIT (a long table needs the room).
CAP = 30
LIMIT = 65000
STATUSES = {"PROVEN", "EXCLUDED", "SPLIT", "UNPROVEN"}
SHA = re.compile(r"[0-9a-f]{40}")
AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\dZ")
RUN = re.compile(r"https://[^\s()<>\[\]]+/actions/runs/\d+")
REQUIREMENT = re.compile(ID)


def history(body):
    """The entries of a previous comment's data block, newest first; [] when it has none or one not of this
    module's shape (any entry unreadable discards the block, so a half-read history is never carried)."""
    lines = (body or "").rstrip().split("\n")
    match = BLOCK.fullmatch(lines[-1].strip()) if lines else None
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return []
    commits = data.get("commits") if isinstance(data, dict) and data.get("v") == 1 else None
    if not isinstance(commits, list) or not all(readable(entry) for entry in commits):
        return []
    return commits


def readable(entry):
    if not isinstance(entry, dict) or set(entry) != {"sha", "at", "run", "verdicts"}:
        return False
    verdicts = entry["verdicts"]
    return (isinstance(entry["sha"], str) and SHA.fullmatch(entry["sha"]) is not None
            and isinstance(entry["at"], str) and AT.fullmatch(entry["at"]) is not None
            and isinstance(entry["run"], str) and RUN.fullmatch(entry["run"]) is not None
            and (verdicts is None or isinstance(verdicts, dict) and all(
                isinstance(rid, str) and REQUIREMENT.fullmatch(rid) and status in STATUSES
                for rid, status in verdicts.items())))


def recorded(commits, entry):
    """`entry` first, then the earlier commits without its own (a re-run on a commit replaces that commit's entry),
    at most CAP in all."""
    return ([entry] + [old for old in commits if old["sha"] != entry["sha"]])[:CAP]


def caught(commits):
    """(id, sha unproven at, sha proven at) for each requirement UNPROVEN on one graded commit and PROVEN or
    EXCLUDED on the next graded one, oldest first: what the audit caught and the pull request then fixed."""
    graded = [entry for entry in reversed(commits) if entry["verdicts"] is not None]
    return [(rid, before["sha"], after["sha"]) for before, after in zip(graded, graded[1:])
            for rid, status in sorted(before["verdicts"].items())
            if status == "UNPROVEN" and after["verdicts"].get(rid) in ("PROVEN", "EXCLUDED")]


def earlier(commits, head, repo_url):
    """The section for a person: every entry but the head's own, whose result is the table above."""
    rows = [entry for entry in commits if entry["sha"] != head]
    if not rows:
        return ""
    lines = ["", "### Earlier commits", "",
             "What the audit found on each earlier commit of this pull request, newest first: a requirement "
             "unproven there and proven above was fixed since.", "",
             "| Commit, audited at (UTC) | Proven or excluded | Unproven there |", "|---|---|---|"]
    for entry in rows:
        when = "[%s %s](%s/commit/%s)" % (entry["at"][:10], entry["at"][11:16], repo_url, entry["sha"])
        verdicts = entry["verdicts"]
        if verdicts is None:
            lines.append("| %s | not graded | |" % when)
            continue
        graded = [rid for rid, status in verdicts.items() if status != "SPLIT"]
        unproven = [rid for rid in graded if verdicts[rid] == "UNPROVEN"]
        lines.append("| %s | %d of %d | %s |" % (when, len(graded) - len(unproven), len(graded), ", ".join(unproven) or "none"))
    return "\n".join(lines)


def render(table, head, run, commits):
    """The comment's body: the marker, this run's table, its commit and run, the earlier commits and the block."""
    repo_url = run.split("/actions/runs/")[0]
    while True:
        block = json.dumps({"v": 1, "commits": commits}, separators=(",", ":"))
        body = "%s\n%s\n\nOn [this commit](%s/commit/%s). [The run and its evidence](%s). Each PROVEN quote was " \
               "checked against the file; a grade is the model's, its quotes are not.\n%s\n\n" \
               "<!-- vibe-verifier:proof-audit-history %s -->" % (
                   MARKER, table.rstrip("\n"), repo_url, head, run, earlier(commits, head, repo_url),
                   block.replace("<", "\\u003c").replace(">", "\\u003e"))
        if len(body) <= LIMIT or len(commits) <= 1:
            return body
        commits = commits[:-1]


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--table", required=True, help="the gate's per-ID table")
    parser.add_argument("--verdicts", required=True, help="the gate's {id: status} file; absent when nothing was graded")
    parser.add_argument("--previous", required=True, help="the earlier comment as the API returns it; absent when there is none")
    parser.add_argument("--head", required=True, help="the commit this run judged")
    parser.add_argument("--run", required=True, help="this run's URL")
    parser.add_argument("--out", required=True, help="write {\"body\": ...} here, for `gh api --input`")
    parser.add_argument("--at", default=None, help=argparse.SUPPRESS)  # tests fix the time
    args = parser.parse_args(argv)
    with open(args.table, encoding="utf-8") as handle:
        table = handle.read()
    verdicts = read_json(args.verdicts)
    previous = read_json(args.previous)
    at = args.at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    entry = {"sha": args.head, "at": at, "run": args.run, "verdicts": verdicts if isinstance(verdicts, dict) else None}
    commits = history(previous.get("body") if isinstance(previous, dict) else None)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"body": render(table, args.head, args.run, recorded(commits, entry))}, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
