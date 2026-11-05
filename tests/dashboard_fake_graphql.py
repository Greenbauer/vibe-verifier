"""GraphQL for tests/dashboard_fake_github.py: GitHub's point price for a query, and its answers."""

from __future__ import annotations

import re
from datetime import timedelta


def graphql_cost(query: str, variables: dict[str, str]) -> int:
    """GitHub's price for a query: every connection costs one request per parent node asked for
    (the product of the `first`/`last` sizes above it), and 100 requests are one point, at least 1.
    docs.github.com, "Rate limits and query limits for the GraphQL API", "Calculating a rate limit score"."""
    requests, stack, pending = 0, [1], 1
    for token in re.findall(r"[A-Za-z_$][\w$]*\s*\([^)]*\)|[{}]", query):
        if token == "{":
            stack.append(pending)
        elif token == "}":
            stack.pop()
            pending = stack[-1]
        else:
            size = re.search(r"\b(?:first|last)\s*:\s*(\$?\w+)", token)
            if size and not token.startswith(("query", "mutation")):
                value = size.group(1)
                count = int(variables.get(value[1:], 100) if value.startswith("$") else value)
                requests += stack[-1]
                pending = stack[-1] * count
            else:
                pending = stack[-1]
    return max(1, round(requests / 100))


def open_pulls(fake) -> dict:
    """The repository's open pull requests, as the pull-signals query reads them."""
    pushed = fake.iso(fake.now - timedelta(hours=1))
    commit = {"commit": {"messageHeadline": "fix: the thing", "committedDate": pushed, "pushedDate": pushed,
                         "parents": {"totalCount": 1}}}
    node = {"number": 3, "baseRefName": "main", "headRefName": "fix/thing", "reviewDecision": None,
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "commits": {"nodes": [commit]}, "mergeStateStatus": "CLEAN", "isDraft": False}
    return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [node]}}}


def head_node(fake) -> dict:
    """The repository's open pull request, as the head search reads it."""
    return {"number": 3, "title": "Fix the thing", "isDraft": False, "url": "https://github.com/%s/pull/3" % fake.repo,
            "createdAt": fake.iso(fake.now - timedelta(days=1)), "author": {"login": fake.owner},
            "headRefOid": fake.sha, "baseRefName": "main", "isCrossRepository": False,
            "headRef": {"compare": {"aheadBy": 1}}, "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
            "reviewDecision": None, "repository": {"nameWithOwner": fake.repo, "defaultBranchRef": {"name": "main"}},
            "statusCheckRollup": {"state": fake.rollup}}


def answer(fakes: list, query: str, variables: dict[str, str], cost: int) -> dict:
    """Answer one query across every repository of the owner."""
    first = fakes[0]
    limit = {"cost": cost, "remaining": 4900, "limit": 5000, "resetAt": first.iso(first.now + timedelta(hours=1))}
    if "pullRequests(" in query:
        named = [fake for fake in fakes if fake.repo == "%s/%s" % (variables.get("owner"), variables.get("name"))]
        return {"data": {**open_pulls(named[0] if named else first), "rateLimit": limit}}
    if "search(" in query:
        nodes = [head_node(fake) for fake in fakes]
        return {"data": {"search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": nodes}, "rateLimit": limit}}
    return {"data": {"repositoryOwner": {"__typename": "User", "login": first.owner}, "rateLimit": limit}}
