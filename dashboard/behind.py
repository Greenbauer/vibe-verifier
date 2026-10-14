"""How far a pull request's head is behind its repository's default branch, from the head search."""

from __future__ import annotations


def behind_default(node: dict) -> tuple[int | None, str | None]:
    """Commits on the default branch this head does not have, and that branch's name.

    `headRef.compare(headRef: "HEAD")` takes the head as its base, so its aheadBy is how far the
    default branch has moved past it. None when that is not the base: a fork resolves HEAD in
    the fork, and a pull request into another branch is measured against that branch, not this one."""
    repo = node.get("repository") if isinstance(node.get("repository"), dict) else {}
    default = repo.get("defaultBranchRef") if isinstance(repo.get("defaultBranchRef"), dict) else {}
    name = default.get("name") if isinstance(default.get("name"), str) else None
    head = node.get("headRef") if isinstance(node.get("headRef"), dict) else {}
    compare = head.get("compare") if isinstance(head.get("compare"), dict) else {}
    count = compare.get("aheadBy")
    if (name is None or node.get("isCrossRepository") is not False or node.get("baseRefName") != name
            or isinstance(count, bool) or not isinstance(count, int) or count < 0):
        return None, name
    return count, name


def stamp_behind(pull: dict, head: dict | None) -> dict:
    """Copy the head reading's distance from the default branch onto the row it describes."""
    if isinstance(pull, dict) and isinstance(head, dict) and "behind_by" in head:
        pull["behind_by"], pull["default_branch"] = head["behind_by"], head.get("default_branch")
    return pull
