"""The count ratchet of a tool descriptor (`"ratchet": "count"`, gates/_descriptors.py).

The tool runs on the judged files at HEAD and again on the same files as the merge base has them (a file the pull
request moved at its old path, one it added not at all). A file blocks on a rule when it has more findings of that rule
at HEAD than at the merge base, and then every finding of that rule in the file is reported, with both counts, since a
tool does not say which one is new. Editing, moving or fixing code that already had a finding never blocks; fixing one
and adding one of the same rule in the same file nets zero. A finding's rule is the one its parser names (gates/_parsers.py
sets `rule`), else its message.
"""
import os
import subprocess
import tempfile
from collections import Counter

from _contract import Finding, git, renamed


def rule(finding):
    return getattr(finding, "rule", None) or finding.message


def _write_base(repo, start, moved, files, snapshot):
    """Write each of `files` under `snapshot` as the merge base `start` has it; return those it has."""
    at_base = []
    for path in files:
        origin = moved.get(path, path)
        if subprocess.run(["git", "-C", repo, "cat-file", "-e", "%s:%s" % (start, origin)], capture_output=True).returncode:
            continue
        target = os.path.join(snapshot, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(git(repo, "show", "%s:%s" % (start, origin)))
        at_base.append(path)
    return at_base


def count_ratchet(repo, base, files, head, scan):
    """The findings of `head` (the tool's at HEAD) that block: those of each (file, rule) with more of them than at the
    merge base. `scan(root, paths)` runs the tool on `paths` written under `root`."""
    start = git(repo, "merge-base", base, "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="vv-base-") as snapshot:
        at_base = _write_base(repo, start, renamed(repo, base), files, snapshot)
        before = scan(snapshot, at_base) if at_base else []
    was = Counter((finding.path, rule(finding)) for finding in before)
    now = Counter((finding.path, rule(finding)) for finding in head)
    return [Finding("%s (%s results in this file: %d at the base, %d now)" % (finding.message, rule(finding),
                                                                          was[key], now[key]), finding.path, finding.line)
            for finding in sorted(head, key=lambda f: (f.path or "", f.line or 0, f.message))
            for key in [(finding.path, rule(finding))] if now[key] > was[key]]
