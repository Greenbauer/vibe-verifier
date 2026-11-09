"""Draft pull requests' test checks that have not run yet."""

from __future__ import annotations


# The job each workflow names for "every test job succeeded", in the catalog's check names
# (docs/check-names.md): `All tests`, and `tests`, its old name kept while rulesets move.
TEST_SUMMARIES = frozenset({"All tests", "tests"})


def _not_run(jobs: list[dict]) -> list[dict]:
    """One run's jobs to mark "draft", or none when the run does not qualify (see mark_draft_not_run)."""
    summaries = [job for job in jobs if job.get("name") in TEST_SUMMARIES and job.get("category") == "failed"]
    others = [job for job in jobs if job.get("name") not in TEST_SUMMARIES]
    states = {job.get("category") for job in others}
    if not summaries or "skipped" not in states or states & {"failed", "cancelled"}:
        return []
    return summaries + [job for job in others if job.get("category") == "skipped"]


def mark_draft_not_run(checks: list[dict], runs: list[dict]) -> None:
    """On a draft pull request, mark the test jobs a workflow skipped, and the test summary that failed
    only because of that, as "draft" (not run until the pull request is marked ready for review).

    A run qualifies when one of its TEST_SUMMARIES jobs failed, at least one job was skipped, and no
    other job failed or was cancelled: then the summary has nothing to report but the skip. A job
    that ran and failed keeps the run, and its summary, red. Changes the rows in place."""
    marked = set()
    for run in runs:
        for job in _not_run(run.get("jobs") or []):
            job["category"] = "draft"
            marked.add(job.get("id"))
    for row in checks:
        if row.get("id") in marked:
            row["category"] = "draft"
