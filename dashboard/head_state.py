"""Every open pull request's head and check rollup, in one or two GraphQL calls.

The detail budget does not gate this read. GitHub priced the search below at 1 point for a
page of 100 on 2026-10-08 (`rateLimit.cost`), against the 5,000 point hour. The detail query,
a page of 50 with review threads and commits, cost 61 points on the same day.

A beat asks again on that interval while nobody is refreshing. One call an interval is
20 calls and 20 points an hour when the page costs 1. A second page, for more than 100 open
pull requests, doubles that. Either way it is under one percent of the hourly point budget.
"""

from __future__ import annotations

import copy
from datetime import datetime

from .gh_api import ApiError
from .util import iso_time, parse_time

# How often the head beat runs, and how long a check reading may stay green.
BEAT_SECONDS = 180
FRESH_SECONDS = BEAT_SECONDS
PAGE = 100
PAGES = 2
_KINDS = {"changed": 0, "new": 1, "kept": 2}

HEADS = """
query($search: String!, $count: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  search(query: $search, type: ISSUE, first: $count, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        headRefOid
        mergeStateStatus
        mergeable
        reviewDecision
        repository { nameWithOwner }
        statusCheckRollup { state }
      }
    }
  }
}
"""

OWNER = """
query($login: String!) {
  rateLimit { cost remaining resetAt }
  repositoryOwner(login: $login) { __typename login }
}
"""


def search_text(owner: str, kind: str) -> str:
    qualifier = "org" if kind == "Organization" else "user"
    return "is:pr is:open %s:%s" % (qualifier, owner)


def calls_per_hour(calls: int) -> int:
    """How many of these calls a quiet hour makes, at one beat per interval."""
    return calls * (3600 // BEAT_SECONDS)


def head_key(repository: object, number: object) -> tuple | None:
    if not isinstance(repository, str) or isinstance(number, bool) or not isinstance(number, int):
        return None
    return repository.casefold(), number


def same_head(stored: dict | None, head: dict | None) -> bool:
    """True when this reading's head sha and rollup are the ones the detail was built from."""
    if not stored or not head or stored.get("rollup_known") is not True:
        return False
    return stored.get("head_sha") == head.get("sha") and stored.get("rollup") == head.get("rollup")


def classify(stored: dict | None, head: dict | None) -> str:
    if stored is None:
        return "new"
    return "kept" if same_head(stored, head) else "changed"


def _cost(data: dict) -> int:
    rate = data.get("rateLimit")
    cost = rate.get("cost") if isinstance(rate, dict) else None
    return cost if isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0 else 0


def _cursor(page: dict) -> str | None:
    if page.get("hasNextPage") is not True:
        return None
    cursor = page.get("endCursor")
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512 or any(char.isspace() for char in cursor):
        return ""
    return cursor


def _one_head(node: dict) -> tuple | None:
    number, sha = node.get("number"), node.get("headRefOid")
    repo = node.get("repository") if isinstance(node.get("repository"), dict) else {}
    name = repo.get("nameWithOwner")
    key = head_key(name, number)
    if key is None or not isinstance(sha, str):
        return None
    rollup = node.get("statusCheckRollup")
    state = rollup.get("state") if isinstance(rollup, dict) else None
    if state is not None and not isinstance(state, str):
        return None
    return key, {"sha": sha, "rollup": state, "merge_state": node.get("mergeStateStatus"),
                 "mergeable": node.get("mergeable"), "review": node.get("reviewDecision")}


def read_head_page(payload: object) -> tuple[dict, str | None, int] | None:
    """Heads keyed by repository and number, the next cursor, and this page's point cost."""
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    data = payload.get("data")
    search = data.get("search") if isinstance(data, dict) else None
    nodes = search.get("nodes") if isinstance(search, dict) else None
    page = search.get("pageInfo") if isinstance(search, dict) and isinstance(search.get("pageInfo"), dict) else {}
    if not isinstance(data, dict) or not isinstance(nodes, list):
        return None
    cursor = _cursor(page)
    if cursor == "":
        return None
    found = {}
    for node in nodes:
        parsed = _one_head(node) if isinstance(node, dict) else None
        if parsed:
            found[parsed[0]] = parsed[1]
    return found, cursor, _cost(data)


def owner_kind(payload: object) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) and not payload.get("errors") else None
    owner = data.get("repositoryOwner") if isinstance(data, dict) else None
    kind = owner.get("__typename") if isinstance(owner, dict) else None
    return kind if kind in ("User", "Organization") else None


def _graphql(api, query: str, variables: dict) -> dict:
    graphql = getattr(api, "graphql", None)
    if not callable(graphql):
        raise ApiError("unavailable")
    return graphql(query, variables, budgeted=False)


def load_heads(api, owner: str, kind: str, count: int) -> dict:
    """Every open pull request's head. Raises ApiError when a page is unusable."""
    found: dict = {}
    cursor = None
    page_size = min(PAGE, max(count, 1))
    for _ in range(PAGES):
        variables = {"search": search_text(owner, kind), "count": page_size}
        if cursor:
            variables["cursor"] = cursor
        page = read_head_page(_graphql(api, HEADS, variables))
        if page is None:
            raise ApiError("invalid_response")
        found.update(page[0])
        api.add_points(page[2])
        cursor = page[1]
        if not cursor:
            return found
        page_size = PAGE
    return found


def known_kind(collector) -> str:
    if collector._kind in ("User", "Organization"):
        return collector._kind
    payload = _graphql(collector.api, OWNER, {"login": collector.config.owner})
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        collector.api.add_points(_cost(data))
    kind = owner_kind(payload)
    if kind is None:
        raise ApiError("invalid_response")
    collector._remember_kind({"type": kind, "login": collector.config.owner})
    return kind


def mark_urgent(collector, heads: dict) -> None:
    """A head or rollup that no longer matches its detail is detailed before anything else."""
    detail, urgent = getattr(collector, "_detail", None), getattr(collector, "_urgent", None)
    if detail is None or urgent is None:
        return
    for key, stored in detail.items():
        if not same_head(stored, heads.get(key)):
            urgent.add(key)


def read_cached_heads(collector, github: dict) -> dict:
    """The beat's one read. Returns the calls and points this read added."""
    before_calls = getattr(collector.api, "head_calls", 0)
    before_points = getattr(collector.api, "graphql_points", 0)
    count = sum(len(row.get("pulls") or []) for row in github.get("repositories") or [])
    sample_heads(collector, count)
    if not collector._heads_ok:
        raise ApiError("unavailable")
    return {"heads": collector._heads,
            "calls": getattr(collector.api, "head_calls", 0) - before_calls,
            "points": getattr(collector.api, "graphql_points", 0) - before_points}


def sample_heads(collector, count: int) -> None:
    """Read heads when count is positive. No GraphQL client leaves the flag false."""
    collector._heads, collector._heads_ok = {}, False
    if count < 1:
        collector._heads_ok = True
        return
    collector._heads = load_heads(collector.api, collector.config.owner, known_kind(collector), count)
    collector._heads_ok = True
    mark_urgent(collector, collector._heads)


def queue_pulls(listings: dict, detail: dict, heads: dict, urgent: set, rank: dict) -> list:
    """Changed heads first, then never detailed, then detail whose head still matches."""
    queued = []
    for repository, rows in listings.items():
        for row in rows:
            key = head_key(repository, row.get("number"))
            kind = "changed" if key in urgent else classify(detail.get(key), heads.get(key) if key else None)
            number = row.get("number") if isinstance(row.get("number"), int) else 0
            queued.append((_KINDS[kind], rank.get(repository, 0), number, repository, row, kind, key))
    queued.sort(key=lambda item: item[:3])
    return [item[3:] for item in queued]


def remember_pull(collector, key, pull: dict) -> None:
    """A pull request detailed on this pass. A rollup that is not SUCCESS cannot be green."""
    head = collector._heads.get(key) if key else None
    known = bool(head and head.get("sha") == pull.get("head_sha"))
    rollup = head.get("rollup") if known else None
    if known and rollup not in (None, "SUCCESS"):
        pull["merge_ready"] = False
    pull["checks_sampled_at"] = iso_time(collector.clock())
    pull["stale"] = False
    pull.pop("checks_age_seconds", None)
    pull["rollup"] = rollup
    pull["rollup_known"] = known
    if not key:
        return
    collector._detail[key] = {"head_sha": pull.get("head_sha"), "rollup": rollup,
                              "rollup_known": known, "pull": copy.deepcopy(pull)}
    collector._urgent.discard(key)
    read_now = getattr(collector, "_read_now", None)
    if isinstance(read_now, set):
        read_now.add(key)


def withheld(pull: dict, now: datetime) -> dict:
    """The same checks, not green, with the age of the check reading."""
    aged = copy.deepcopy(pull)
    aged["merge_ready"] = False
    aged["stale"] = True
    sampled = parse_time(pull.get("checks_sampled_at"))
    if sampled is not None:
        aged["checks_age_seconds"] = max(0, int((now - sampled).total_seconds()))
    return aged


# Shown while the last checks are still on screen and a newer head or rollup is waiting for detail.
PREVIOUS_PUSH = "Checks shown are from the previous push. Newer checks are loading."
BEFORE_CHANGE = "Checks shown are from before the check state changed. Newer checks are loading."
_ROLLUP_CATEGORY = {"SUCCESS": "success", "FAILURE": "failed", "ERROR": "failed",
                    "PENDING": "pending", "EXPECTED": "pending"}


def rollup_category(head: dict | None) -> str | None:
    """The cheap pass's overall check state, for a pull request this process has not detailed."""
    if not isinstance(head, dict):
        return None
    return _ROLLUP_CATEGORY.get(head.get("rollup"))


def behind(pull: dict, head: dict, now: datetime) -> dict:
    """Keep the last checks, say they predate this head or rollup, and do not paint green."""
    aged = withheld(pull, now)
    moved = isinstance(aged.get("head_sha"), str) and aged.get("head_sha") != head.get("sha")
    aged["attention"] = True
    aged["attention_reason"] = PREVIOUS_PUSH if moved else BEFORE_CHANGE
    aged["head_changed"] = moved
    return aged


def show_stored(stored: dict | None, head: dict | None, now: datetime) -> dict | None:
    """The last reading, or None when this pull request has never been detailed."""
    pull = stored.get("pull") if isinstance(stored, dict) else None
    if not isinstance(pull, dict):
        return None
    if isinstance(head, dict) and head_differs(pull, head):
        return behind(pull, head, now)
    return withheld(pull, now)


# A miss of one of these keeps the last checks. Access failures still replace the row.
_KEEP_LAST = {"request_budget_exhausted", "rate_limited", "unavailable", "invalid_response"}


def stored_or_new(collector, repository, row, code, blank):
    """Last checks when this miss may keep them, otherwise the caller's empty row."""
    key = head_key(repository, row.get("number"))
    stored = collector._detail.get(key) if key and code in _KEEP_LAST else None
    kept = show_stored(stored, collector._heads.get(key) if key else None, collector.clock()) if stored else None
    return kept if kept is not None else blank()


def note_detail_time(collector, repository, pulls) -> None:
    """Stamp a repository only when this pass read one of its pull requests."""
    fresh = getattr(collector, "_read_now", ())
    if any(pull.get("source_error") == "request_budget_exhausted" for pull in pulls):
        collector._detailed_at.pop(repository, None)
        return
    if any(head_key(repository, pull.get("number")) in fresh for pull in pulls):
        collector._detailed_at[repository] = collector.clock()


def head_differs(pull: dict, head: dict | None) -> bool:
    """True when this head's sha or rollup is not the one the stored checks describe."""
    if not isinstance(head, dict):
        return False
    sha = pull.get("head_sha")
    if isinstance(sha, str) and sha != head.get("sha"):
        return True
    return pull.get("rollup_known") is True and pull.get("rollup") != head.get("rollup")


def reconcile_pull(pull: dict, head: dict | None, now: datetime) -> dict:
    """A beat did not reload checks, so nothing it returns is green."""
    if not isinstance(pull, dict) or ("merge_ready" not in pull and "checks_sampled_at" not in pull):
        return pull
    if isinstance(head, dict) and head_differs(pull, head):
        return behind(pull, head, now)
    if pull.get("merge_ready") or pull.get("checks_sampled_at") or pull.get("stale"):
        return withheld(pull, now)
    return pull


def apply_head_reading(github: dict, heads: dict, now: datetime, calls: int, points: int) -> dict:
    for row in github.get("repositories") or []:
        repo = row.get("repository")
        row["pulls"] = [reconcile_pull(pull, heads.get(head_key(repo, pull.get("number"))), now)
                        for pull in row.get("pulls") or [] if isinstance(pull, dict)]
    reading = {"sampled_at": iso_time(now), "calls": calls, "points": points,
               "beat_seconds": BEAT_SECONDS, "calls_per_hour": calls_per_hour(calls),
               "points_per_hour": calls_per_hour(points)}
    github["heads_sampled_at"] = reading["sampled_at"]
    github["head_reading"] = reading
    api = github.setdefault("api", {})
    api.update(head_calls=calls, graphql_points=points, head_calls_per_hour=reading["calls_per_hour"],
               graphql_points_per_hour=reading["points_per_hour"], beat_seconds=BEAT_SECONDS)
    return github


def reading_from(collector, now: datetime) -> dict:
    calls, points = getattr(collector.api, "head_calls", 0), getattr(collector.api, "graphql_points", 0)
    return {"sampled_at": iso_time(now) if collector._heads_ok else None, "calls": calls, "points": points,
            "beat_seconds": BEAT_SECONDS, "calls_per_hour": calls_per_hour(calls),
            "points_per_hour": calls_per_hour(points)}


def withhold_if_old(pull: dict, now: datetime, repo_stale: bool) -> None:
    """A check reading older than one beat is not green. A fresh one is left alone."""
    if not isinstance(pull, dict) or ("merge_ready" not in pull and "checks_sampled_at" not in pull):
        return
    sampled = parse_time(pull.get("checks_sampled_at"))
    if sampled is None:
        if pull.get("merge_ready"):
            pull["merge_ready"] = False
            pull["stale"] = True
        return
    age = (now - sampled).total_seconds()
    if 0 <= age <= FRESH_SECONDS and not pull.get("stale") and not repo_stale:
        return
    pull["merge_ready"] = False
    pull["stale"] = True
    if age > FRESH_SECONDS:
        pull["checks_age_seconds"] = int(age)


def withhold_stale_pulls(github: dict, now: datetime) -> None:
    for row in github.get("repositories") or []:
        for pull in row.get("pulls") or []:
            withhold_if_old(pull, now, bool(row.get("stale")))
