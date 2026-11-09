"""Draft pull requests' test checks that have not run yet."""

from __future__ import annotations


# The job each workflow names for "every test job succeeded", in the catalog's check names
# (docs/check-names.md): `All tests`, and `tests`, its old name kept while rulesets move.
TEST_SUMMARIES = frozenset({"All tests", "tests"})


def mark_draft_not_run(checks: list[dict], runs: list[dict]) -> None:
    """On a draft pull request, mark the test jobs a workflow skipped, and the test summary that failed
    only because of that, as "draft" (not run until the pull request is marked ready for review).

    A run qualifies when one of its TEST_SUMMARIES jobs failed, at least one job was skipped, and no
    other job failed or was cancelled: then the summary has nothing to report but the skip. A job
    that ran and failed keeps the run, and its summary, red. Changes the rows in place."""
    marked = set()
    for run in runs:
        jobs = run.get("jobs") or []
        summaries = [job for job in jobs if job.get("name") in TEST_SUMMARIES and job.get("category") == "failed"]
        others = [job for job in jobs if job.get("name") not in TEST_SUMMARIES]
        if (not summaries or not any(job.get("category") == "skipped" for job in others)
                or any(job.get("category") in {"failed", "cancelled"} for job in others)):
            continue
        for job in summaries + [job for job in others if job.get("category") == "skipped"]:
            job["category"] = "draft"
            marked.add(job.get("id"))
    for row in checks:
        if row.get("id") in marked:
            row["category"] = "draft"
