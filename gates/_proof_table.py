"""The per-ID table gates/proof_audit.py writes, which actions/proof-audit posts as the pull request's proof-audit
comment, and the one-line cell every such comment's text goes through (the copy audit and the QA review use it too)."""
import os
import re


def proof_summary(grade):
    return "; ".join("%s: %s" % (case["case"], ", ".join(proof_cite(proof) for proof in case["proof"]))
                     for case in grade["cases"])


def proof_cite(proof):
    """`file:line` (test), and the line the grader cited when the gate found the quote elsewhere in that test."""
    moved = "; cited :%d" % proof["cited_line"] if "cited_line" in proof else ""
    return "`%s:%d` (%s%s)" % (proof["file"], proof["line"], proof["test"].strip(), moved)


def without_runner_paths(text):
    """`text` with this runner's own folders (workspace, temp, home) dropped from every path that starts with one:
    only where a path starts, so a repository path or a route that merely contains the same name stays."""
    roots = {os.environ.get(name, "").rstrip("/") for name in ("GITHUB_WORKSPACE", "RUNNER_TEMP", "HOME", "TMPDIR")}
    for root in sorted(filter(None, roots), key=len, reverse=True):
        text = re.sub(r"(?<![\w./~-])" + re.escape(root) + "/", "", text)
    return text


def cell(text, limit=600):
    """One line of the comment, for a person: in text the gate did not write (a CLI's error, a grade) a URL goes to
    the job log, and the rendering runner's folders are dropped from a path (docs/comments.md)."""
    text = re.sub(r"https?://\S+", "(link in the job log)", " ".join(str(text).split()).replace("|", "\\|"))
    text = without_runner_paths(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def table(rows, note=None):
    proven = sum(1 for _, status, _ in rows if status in ("PROVEN", "EXCLUDED"))
    split = sum(1 for _, status, _ in rows if status == "SPLIT")
    note = note or "%d of %d requirements proven or excluded%s" % (proven, len(rows), ", %d split off" % split if split else "")
    lines = ["## Proof audit: %s" % cell(note), ""]
    if rows:
        lines += ["| ID | Verdict | Proof, or what is missing |", "|---|---|---|"]
        lines += ["| %s | %s | %s |" % (rid, status, cell(detail)) for rid, status, detail in rows]
    return "\n".join(lines) + "\n"


def write_table(path, text):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
