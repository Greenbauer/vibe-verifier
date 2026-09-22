"""Which workflow files a workflow linter judges.

A pull request is judged on the workflows it added or changed since the base, so a finding in a
workflow nobody touched is never this PR's; `--all` lints every tracked workflow, for an audit or
a first subscription.
"""
from _contract import git

WORKFLOWS = ".github/workflows"


def workflow_files(repo, base, everything):
    if everything:
        names = git(repo, "ls-files", "-z", "--", WORKFLOWS).split("\0")
    else:
        names = git(repo, "diff", "--name-only", "--diff-filter=ACMR", base + "...HEAD", "--", WORKFLOWS).splitlines()
    return sorted(name for name in names if name and name.rsplit(".", 1)[-1] in ("yml", "yaml"))
