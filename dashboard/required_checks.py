"""Required checks the base branch still expects on a pull request head.

Rulesets and classic branch protection are separate GitHub reads. A check either source requires,
and that has not reported on the head, is one expected row. Classic protection also says whether
the branch restricts who may push, which decides how GitHub's merge state is read.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from .gh_api import ApiError
from .util import parse_time

WORKFLOW_GAP = "Required workflow, not run at its current pin"
# Not a GitHub rule type: the row classic_protection adds for a branch that restricts who may push.
PUSH_RESTRICTED = "push_restricted"


def required_contexts(protection: dict) -> list[str]:
    """Context names from classic branch protection, preferring the checks list GitHub still fills."""
    checks = protection.get("checks")
    if isinstance(checks, list) and checks:
        return [check_context(item) for item in checks]
    raw = protection.get("contexts") or []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ApiError("invalid_response")
    return raw


def check_context(item: object) -> str:
    context = item.get("context") if isinstance(item, dict) else None
    if not isinstance(context, str) or not context:
        raise ApiError("invalid_response")
    return context


def history_path(repository: str, rule: dict, key: tuple) -> str:
    if rule.get("ruleset_source_type") == "Organization":
        return "orgs/%s/rulesets/%s/history" % (rule.get("ruleset_source"), key[0])
    return "repos/%s/rulesets/%s/history" % (repository, key[0])


def pinned_shas(state: dict) -> dict:
    pins = {}
    for old in state.get("rules") or []:
        if old.get("type") != "workflows":
            continue
        for pinned in (old.get("parameters") or {}).get("workflows") or []:
            pins[(pinned.get("path"), pinned.get("repository_id"))] = pinned.get("sha")
    return pins


def pin_start(versions: list[dict], api, base: str, identity: tuple, sha: object) -> datetime | None:
    from .github import _time_key

    since = None
    for version in sorted(versions, key=lambda row: _time_key(row, "updated_at"), reverse=True):
        pins = pinned_shas(api.one("%s/%s" % (base, version.get("version_id"))).get("state") or {})
        if pins.get(identity) != sha:
            break
        since = parse_time(version.get("updated_at"))
    return since


def remembered_pin(collector, key: tuple, sha: object):
    cached = collector._pins.get(key)
    if cached and cached[0] == sha:
        return cached[1]
    return None


def pin_since(collector, repository: str, rule: dict, workflow: dict) -> datetime | None:
    """When the ruleset started requiring this workflow at its current pinned SHA.

    A required workflow runs at the SHA pinned when its triggering event fired, so after the pin
    moves GitHub waits for a new run and ignores older ones. A given pin's start never changes,
    so it is cached until the pin moves."""
    from .github import PIN_UNKNOWN, _endpoint

    key = (rule.get("ruleset_id"), workflow.get("path"), workflow.get("repository_id"))
    sha = workflow.get("sha")
    cached = remembered_pin(collector, key, sha)
    if cached is not None:
        return cached
    base = history_path(repository, rule, key)
    try:
        versions = collector.api.items(_endpoint(base, per_page=100))
    except ApiError as error:
        # Organization ruleset history needs administration write, which this token does not hold.
        # Without the pin's start, any run of the required workflow counts.
        if error.code != "forbidden":
            raise
        collector._pins[key] = (sha, PIN_UNKNOWN)
        return PIN_UNKNOWN
    since = pin_start(versions, collector.api, base, key[1:], sha)
    if since:
        collector._pins[key] = (sha, since)
    return since


def status_gaps(parameters: dict, reported: set) -> list[tuple]:
    return [(item["context"], "Required status check")
            for item in parameters.get("required_status_checks") or []
            if item["context"] not in reported]


def workflow_name(path: str, matching: list[dict]) -> str:
    return matching[0]["name"] if matching else path.rsplit("/", 1)[-1]


def workflow_gap(collector, repository: str, rule: dict, workflow: dict, runs: list[dict]):
    from .github import _time_key

    path = workflow["path"]
    matching = [run for run in runs if run["required_workflow"] and run["path"] == path]
    since = pin_since(collector, repository, rule, workflow) if matching else None
    current = since and any(_time_key(run, "created_at") >= since for run in matching)
    if current:
        return None
    return (workflow_name(path, matching), WORKFLOW_GAP)


def rule_gaps(collector, repository: str, rule: dict, reported: set, runs: list[dict]) -> list[tuple]:
    parameters = rule.get("parameters") or {}
    if rule.get("type") == "required_status_checks":
        return status_gaps(parameters, reported)
    if rule.get("type") != "workflows":
        return []
    gaps = []
    for workflow in parameters.get("workflows") or []:
        gap = workflow_gap(collector, repository, rule, workflow, runs)
        if gap:
            gaps.append(gap)
    return gaps


def expected_row(name: str, provider: str) -> dict:
    return {"id": None, "suite_id": None, "name": name[:200], "provider": provider, "status": "expected",
            "conclusion": None, "category": "pending", "started_at": None, "completed_at": None,
            "elapsed_seconds": None, "details_url": None}


def expected_rows(collector, repository: str, rules: list[dict], evidence: list[dict], runs: list[dict]) -> list[dict]:
    """Checks the base branch requires that have not reported on this head.

    GitHub lists these as "Expected" without creating a check run for them. A required workflow run
    from before its pin moved does not count. The same context from two rules is one row."""
    reported = {row["name"] for row in evidence}
    missing = []
    for rule in rules:
        missing += rule_gaps(collector, repository, rule, reported, runs)
    rows, seen = [], set()
    for name, provider in missing:
        if name in seen:
            continue
        seen.add(name)
        rows.append(expected_row(name, provider))
    return rows


def classic_protection(api, repository: str, base: str) -> list[dict]:
    """What classic branch protection adds that the rules API does not return.

    Its required status checks, and one PUSH_RESTRICTED row when it restricts who may push.
    Reading it needs Administration read. A token without it, or a branch with no classic
    protection, leaves both out; the pull request's other evidence still stands."""
    try:
        protection = api.one("repos/%s/branches/%s/protection" % (repository, quote(base, safe="")))
    except ApiError as error:
        if error.code in {"forbidden", "not_found"}:
            return []
        raise
    rules = []
    status = protection.get("required_status_checks")
    contexts = required_contexts(status) if isinstance(status, dict) else []
    if contexts:
        rules.append({"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": context} for context in contexts]}})
    if isinstance(protection.get("restrictions"), dict):
        rules.append({"type": PUSH_RESTRICTED})
    return rules


def branch_rules(api, repository: str, base: str) -> list[dict]:
    """The rules GitHub enforces on a base branch, or none where GitHub refuses to list them.

    Branch rules need only metadata read. A 403 means the plan has no rulesets. Classic branch
    protection is a separate read and can still require checks on that plan."""
    from .github import _endpoint

    try:
        rules = api.items(_endpoint("repos/%s/rules/branches/%s" % (repository, quote(base, safe="")),
                                    per_page=100))
    except ApiError as error:
        if error.code != "forbidden":
            raise
        rules = []
    return rules + classic_protection(api, repository, base)
