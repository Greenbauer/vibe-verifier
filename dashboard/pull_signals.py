"""Latest push, unresolved review threads, and whether a pull request is merge-ready.

The collector asks GitHub once per repository. This module only interprets that
answer: it does not call GitHub.
"""

from __future__ import annotations

import re

from .gh_api import ApiError
from .util import parse_time

# One page of open pull requests: review threads (resolved, and whether anyone
# replied) and the newest commits (message, push time, parent count).
# GitHub prices a query by the page sizes it asks for, not by what comes back: every pull
# request asked for costs about 1.2 points here, so $count is the number that are open.
PAGE = 50
OPEN_PULLS = """
query($owner: String!, $name: String!, $count: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $count, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        baseRefName
        reviewThreads(first: 100) {
          pageInfo { hasNextPage }
          nodes { isResolved comments(first: 1) { totalCount } }
        }
        commits(last: 20) {
          nodes {
            commit {
              messageHeadline
              committedDate
              pushedDate
              parents(first: 1) { totalCount }
            }
          }
        }
      }
    }
  }
}
"""

_KINDS = {
    "feat": "feature", "feature": "feature",
    "fix": "bug fix", "bugfix": "bug fix", "hotfix": "bug fix",
    "refactor": "refactor", "perf": "performance",
    "docs": "docs", "doc": "docs", "test": "test", "tests": "test",
    "chore": "chore", "ci": "ci", "build": "build", "style": "style",
    "revert": "revert", "wip": "work in progress",
}
_CONVENTIONAL = re.compile(
    r"^(?P<type>" + "|".join(_KINDS) + r")(?:\([^)\n]+\))?!?:\s+\S", re.IGNORECASE)
_SYNC_MERGE = re.compile(r"^Merge (?:remote-tracking )?branch '([^']+)'", re.IGNORECASE)
_SYNC_SUBJECT = re.compile(r"^sync(?:ing)? with (\S+)", re.IGNORECASE)
_CURSOR = re.compile(r"[A-Za-z0-9_\-=+/]{1,512}\Z")

MISSING = {"review_threads": None, "unresolved_comments": None, "comments_complete": False,
           "threads_settled": False, "push": None}


def _branch_name(value: str) -> str:
    name = value.strip().strip("'\"")
    for prefix in ("origin/", "refs/heads/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    return name


def _is_base(branch: str, base: str | None) -> bool:
    return _branch_name(branch).lower() == _branch_name(base or "main").lower()


def sync_label(base: str | None) -> str:
    name = _branch_name(base or "main")
    if name.lower() in {"main", "master"}:
        return "sync with main"
    return ("sync with %s" % name)[:40]


def _kind(headline: str, parents: int, base: str | None) -> str:
    text = headline.strip()
    merged = _SYNC_MERGE.match(text)
    if merged and _is_base(merged.group(1), base):
        return sync_label(base)
    subject = _SYNC_SUBJECT.match(text)
    if subject and _is_base(subject.group(1).rstrip(".,:;"), base):
        return sync_label(base)
    conventional = _CONVENTIONAL.match(text)
    if conventional:
        return _KINDS[conventional.group("type").lower()]
    if parents > 1:
        return "merge"
    return "update"


def _one_commit(item: object) -> dict | None:
    commit = item.get("commit") if isinstance(item, dict) else None
    if not isinstance(commit, dict):
        return None
    headline = commit.get("messageHeadline") if isinstance(commit.get("messageHeadline"), str) else ""
    parents = commit.get("parents") if isinstance(commit.get("parents"), dict) else {}
    count = parents.get("totalCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        count = 1
    return {"headline": headline,
            "pushed_at": commit.get("pushedDate") if isinstance(commit.get("pushedDate"), str) else None,
            "committed_at": commit.get("committedDate") if isinstance(commit.get("committedDate"), str) else None,
            "parents": count}


def _commit_rows(node: dict) -> list[dict]:
    connection = node.get("commits")
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    if not isinstance(nodes, list):
        return []
    return [row for item in nodes if (row := _one_commit(item))]


def _dated(commits: list[dict]) -> list[dict]:
    dated = []
    for commit in commits:
        pushed, committed = parse_time(commit.get("pushed_at")), parse_time(commit.get("committed_at"))
        if pushed is not None or committed is not None:
            dated.append({**commit, "pushed": pushed, "committed": committed})
    return dated


def _latest_group(dated: list[dict]) -> tuple[list[dict], str]:
    pushed_rows = [row for row in dated if row["pushed"] is not None]
    if pushed_rows:
        latest = max(row["pushed"] for row in pushed_rows)
        group = [row for row in pushed_rows if row["pushed"] == latest]
        return group, group[0]["pushed_at"]
    latest = max(row["committed"] for row in dated)
    group = [row for row in dated if row["committed"] == latest]
    return group, group[-1]["committed_at"]


def _push_kind(kinds: list[str], sync: str) -> str:
    substantive = [kind for kind in kinds if kind != sync]
    if not substantive:
        return sync if sync in kinds else kinds[-1]
    if len(set(substantive)) == 1:
        return substantive[0]
    return substantive[-1]


def classify_push(commits: list[dict], base: str | None) -> dict | None:
    """The newest push: when it landed, and one label for what it contained."""
    dated = _dated(commits)
    if not dated:
        return None
    group, when = _latest_group(dated)
    kinds = [_kind(str(row.get("headline") or ""), int(row["parents"]), base) for row in group]
    return {"pushed_at": when, "kind": _push_kind(kinds, sync_label(base))}


def thread_summary(nodes: list[dict], complete: bool) -> dict:
    """Unresolved review threads, and whether every thread is resolved and replied to."""
    unresolved = 0
    settled = complete
    for node in nodes:
        resolved = node.get("isResolved")
        if not isinstance(resolved, bool):
            return {**MISSING, "push": None}
        if not resolved:
            unresolved += 1
            settled = False
        comments = node.get("comments") if isinstance(node.get("comments"), dict) else {}
        total = comments.get("totalCount")
        if isinstance(total, bool) or not isinstance(total, int) or total < 2:
            settled = False
    if not complete:
        settled = False
    return {"review_threads": len(nodes), "unresolved_comments": unresolved,
            "comments_complete": complete, "threads_settled": settled, "push": None}


def signals_from_node(node: dict) -> dict:
    threads = node.get("reviewThreads")
    page = threads.get("pageInfo") if isinstance(threads, dict) and isinstance(threads.get("pageInfo"), dict) else {}
    nodes = threads.get("nodes") if isinstance(threads, dict) else None
    if not isinstance(nodes, list) or any(not isinstance(item, dict) for item in nodes):
        summary = dict(MISSING)
    else:
        summary = thread_summary(nodes, page.get("hasNextPage") is False)
    base = node.get("baseRefName") if isinstance(node.get("baseRefName"), str) else None
    summary["push"] = classify_push(_commit_rows(node), base)
    return summary


def comment_count(current: dict | None) -> int | None:
    """Issue comments plus review comments. None when GitHub did not report both."""
    if not isinstance(current, dict):
        return None
    total = 0
    for key in ("comments", "review_comments"):
        value = current.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        total += value
    return total


def merge_ready(evidence_available: bool, draft: bool, merge_state: object,
                threads_settled: bool, categories: list) -> bool:
    """Clean, not a draft, every check green, every review thread resolved and replied to.

    `clean` is GitHub's merge state: not behind the base, not blocked, not conflicting.
    Skipped checks may sit beside at least one success. A check that is not required still counts.
    """
    if not evidence_available or draft or merge_state != "clean" or not threads_settled:
        return False
    return bool(categories) and "success" in categories and set(categories) <= {"success", "skipped"}


def pull_face(signals: dict | None, *, evidence_available: bool, draft: bool, merge_state: object,
              categories: list, comments: int | None) -> dict:
    """The fields the page reads. Missing signals never become a green title or a zero count."""
    signals = signals or MISSING
    return {"merge_ready": merge_ready(evidence_available, draft, merge_state,
                                        bool(signals.get("threads_settled")), categories),
            "review_threads": signals.get("review_threads"),
            "unresolved_comments": signals.get("unresolved_comments"),
            "comments_complete": bool(signals.get("comments_complete")),
            "comment_count": comments, "push": signals.get("push")}


def face_fields(current: dict | None, signals: dict | None, *, evidence: bool, draft: bool,
               checks: list[dict], statuses: list[dict], expected: list[dict]) -> dict:
    merge_state = current.get("mergeable_state") if isinstance(current, dict) else None
    categories = [row.get("category") for row in (*checks, *statuses, *expected)]
    return pull_face(signals, evidence_available=evidence, draft=draft, merge_state=merge_state,
                     categories=categories, comments=comment_count(current))


def load_signals(api, repository: str, open_pulls: int) -> dict[int, dict]:
    """Review threads and the latest push for every open pull request. Empty when the read fails.

    open_pulls is how many the repository has open, which sizes each page."""
    graphql = getattr(api, "graphql", None)
    if not callable(graphql) or open_pulls < 1:
        return {}
    owner, _, name = repository.partition("/")
    found: dict[int, dict] = {}
    cursor = None
    for _ in range(4):
        variables = {"owner": owner, "name": name, "count": min(open_pulls, PAGE)}
        if cursor:
            variables["cursor"] = cursor
        try:
            payload = graphql(OPEN_PULLS, variables)
        except ApiError:
            return {}
        page = read_pull_page(payload)
        if page is None:
            return {}
        found.update(page[0])
        cursor = page[1]
        if not cursor:
            return found
    return found


def read_pull_page(payload: object) -> tuple[dict[int, dict], str | None] | None:
    """Signals keyed by pull number, and the next page cursor. None when the payload is unusable."""
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    connection = repository.get("pullRequests") if isinstance(repository, dict) else None
    if not isinstance(connection, dict):
        return None
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return None
    found = {}
    for node in nodes:
        number = node.get("number") if isinstance(node, dict) else None
        if isinstance(number, int):
            found[number] = signals_from_node(node)
    page = connection.get("pageInfo") if isinstance(connection.get("pageInfo"), dict) else {}
    cursor = page.get("endCursor")
    if page.get("hasNextPage") is True and isinstance(cursor, str) and _CURSOR.fullmatch(cursor):
        return found, cursor
    return found, None
