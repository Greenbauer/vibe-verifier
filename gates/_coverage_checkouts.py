"""The two checkouts of coverage-not-lost: the merge base and HEAD, each a git worktree of its own (hooks off), removed
afterwards, into which the working copy's ignored files are linked and each node_modules directory is mirrored entry by
entry, so both sides run with the dependencies the job installed.
"""
import contextlib
import os
import posixpath
import shutil
import subprocess
import tempfile

from _contract import git

CACHES = {".vite", ".vitest", ".cache"}  # a run writes these: each checkout gets its own


def mirror(source, target):
    """`target` as a directory of links to `source`'s entries: a link entry is copied as the same link, so a
    relative one (a workspace package) resolves inside this checkout; a scope directory is mirrored the same way."""
    os.makedirs(target, exist_ok=True)
    for entry in os.scandir(source):
        link = os.path.join(target, entry.name)
        if entry.name in CACHES or os.path.lexists(link):
            continue
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), link)
        elif entry.name.startswith("@") and entry.is_dir():
            mirror(entry.path, link)
        else:
            os.symlink(entry.path, link)


def link_ignored(repo, tree):
    """Give a checkout the working copy's ignored files (links) and node_modules directories (mirrors)."""
    for path in git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory").split("\0"):
        name = path.rstrip("/")
        source, target = os.path.join(repo, name), os.path.join(tree, name)
        if not name or os.path.lexists(target) or not os.path.isdir(os.path.dirname(target)):
            continue
        if path.endswith("/") and posixpath.basename(name) == "node_modules":
            mirror(source, target)
        elif not path.endswith("/"):
            os.symlink(source, target)


@contextlib.contextmanager
def checkouts(repo, merge_base):
    """(scratch directory, {"base": checkout of the merge base, "head": checkout of HEAD}), removed afterwards."""
    scratch = tempfile.mkdtemp(prefix="vv-coverage-")
    try:
        trees = {}
        for side, revision in (("base", merge_base), ("head", "HEAD")):
            trees[side] = os.path.join(scratch, side)
            git(repo, "-c", "core.hooksPath=" + os.devnull, "worktree", "add", "--detach", "--quiet", trees[side], revision)
            link_ignored(repo, trees[side])
        yield scratch, trees
    finally:
        shutil.rmtree(scratch, ignore_errors=True)  # links are removed, never followed
        subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True)
