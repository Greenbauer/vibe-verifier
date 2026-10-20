"""Whether a pull request changed the Verify section of a feature it re-walks.

The re-walk judges a feature by the steps in the pull request's own copy of its file (docs/feature-map.md),
so a pull request can rewrite the steps that judge it. That is allowed, since a change of behaviour means
a change of steps, and made visible instead: the acceptance-verdict log and the QA review comment say,
per re-walked feature, how its Verify section differs from the one the pull request started from.

The comparison is structural: the non-blank lines of the Verify section (gates/_features.py reads which
lines those are) at the merge base of the base and HEAD, against the working tree, counted as a multiset,
so an edited step is one line added and one removed and a moved line is no change. The merge base is
what the pull request started from, so a change the base branch made since is not counted as its own.
"""
import os
import subprocess
from collections import Counter

from _contract import CannotRun
from _features import map_directory, parse, paths_at


def start_point(repo, base):
    """A merge base of `base` and HEAD; `base` itself when git finds none, as in a shallow clone, where the base
    is the best record left of what the pull request started from."""
    found = subprocess.run(["git", "-C", repo, "merge-base", base, "HEAD"], capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else base


def verify_edits(repo, directory, fids, base):
    """{id: note} for each feature of `fids` whose Verify section the working tree changed since the merge base of
    `base` and HEAD; a feature it did not change, or whose file the working tree lacks, has no entry. Exit 2
    (CannotRun) when git cannot list or show the map at that commit."""
    where = map_directory(directory)
    point = start_point(repo, base)
    at_start = set(paths_at(repo, directory, point))
    notes = {}
    for fid in fids:
        path = "%s/%s.md" % (where, fid)
        if not os.path.isfile(os.path.join(repo, path)):
            continue
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as handle:
            head = parse(handle.read(), fid, path).verify
        if path not in at_start:
            notes[fid] = "this pull request added this feature, so its Verify section (%s) is its own" % lines(len(head))
            continue
        shown = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (point, path)], capture_output=True, text=True,
                               errors="replace")
        if shown.returncode != 0:
            raise CannotRun("git show %s:%s failed: %s" % (point, path, shown.stderr.strip()))
        before, after = Counter(parse(shown.stdout, fid, path).verify), Counter(head)
        added, removed = sum((after - before).values()), sum((before - after).values())
        if added or removed:
            notes[fid] = "this pull request changed its Verify section: %s added, %d removed" % (lines(added), removed)
    return notes


def lines(count):
    return "%d line%s" % (count, "" if count == 1 else "s")
