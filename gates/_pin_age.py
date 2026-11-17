"""How old a catalog commit must be before `apply-down --auto` delivers it (--min-age-hours, actions/pin-bump).

An automatic bump pins only the newest commit of the catalog's main that has been there the minimum age (`aged`),
and the merge pass waits on a catalog bump whose commit is younger (`too_young`), so a bad commit merged to main can
be reverted before any consumer runs it. On main every commit is a squash merge GitHub made (the ruleset allows no
direct push), so its commit time is when it landed.
"""
import time

from _pin_bump import BRANCH_PREFIXES, git, is_sha, open_pulls


def aged(source, tip, hours, clock=time.time):
    """The newest commit on the first-parent line of `tip` in `source` committed at least `hours` ago, or None."""
    cutoff = clock() - hours * 3600
    for line in git(source, "log", "--first-parent", "--format=%H %ct", tip).stdout.splitlines():
        sha, _, at = line.partition(" ")
        if at.isdigit() and int(at) <= cutoff:
            return sha
    return None


def commit_age(source, sha, clock=time.time):
    """Seconds since the commit `sha` (checked to be one, so never an option to git) was committed in `source`, or
    None when it has no readable time."""
    if not is_sha(sha, 7):
        return None
    at = git(source, "log", "-1", "--format=%ct", sha).stdout.strip()
    return clock() - int(at) if at.isdigit() else None


def newer_open_bump(source, repo, target, tip):
    """(number, commit) of an open catalog bump of `repo` (its own branch) to a commit of the catalog checkout `source`
    between `target` and main's `tip`, or None. Such a bump was opened on purpose (by hand, or with a lower age for an
    urgent fix): a scheduled run neither opens an older one beside it nor closes it."""
    prefix = BRANCH_PREFIXES[0]
    for pull in open_pulls(repo):
        head = pull.get("head") or {}
        named = head.get("ref", "")[len(prefix):] if head.get("ref", "").startswith(prefix) else ""
        if ((head.get("repo") or {}).get("full_name") == repo and is_sha(named, 7) and not target.startswith(named)
                and git(source, "merge-base", "--is-ancestor", target, named).returncode == 0
                and git(source, "merge-base", "--is-ancestor", named, tip).returncode == 0):
            return pull["number"], named
    return None


def too_young(source, min_age_hours, clock=time.time):
    """_pin_merge.merge_pass's `young`: for a catalog bump, why the commit its branch names is newer than
    `min_age_hours` in the catalog checkout `source` (one with no readable time counts as new), else None. A wrapper
    bump pins the wrapper's own commits, which this does not judge."""
    def test(prefix, named):
        if prefix != BRANCH_PREFIXES[0] or min_age_hours <= 0:
            return None
        age = commit_age(source, named, clock)
        if age is not None and age >= min_age_hours * 3600:
            return None
        return "%s is %s old; a bump merges once the commit it pins is %gh old" % (
            named, "of unknown age" if age is None else "%.1fh" % (age / 3600), min_age_hours)
    return test
