"""Merged pull requests of the last OUTCOME_DAYS days, each read once after it merges (docs/dashboard.md
"Outcomes view").

A record holds when the pull request left draft and merged, the jobs of its merged head (the page
splits their time with the CI timeline's own rules, dashboard/static/timeline.js), and the runner time
of every job any run on its branch ran while it was open, summed per check. A merged pull request
does not change, so a record is written once, never updated, and dropped once its merge is older than
the window. A pull request whose runs are not all finished gets no record yet and is read again.

The reads have their own small budget (MAX_CALLS a cycle, one cycle every REFRESH_SECONDS) and wait
while the live passes' REST counter is below half, so they never take what the open pull requests need.
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timedelta, timezone

from .bot_runs import bot_job_ran
from .gh_api import PRIVATE_FAILURES, ApiError, GitHubAPI
from .github import GitHubCollector, _endpoint, _newest_attempts, job_row
from .head_state import OWNER, owner_kind
from .util import github_url, iso_time, parse_time

OUTCOME_DAYS = 30
REFRESH_SECONDS = 300
MAX_CALLS = 60
SEARCH_PAGE = 50
# GitHub's search returns at most 1000 results.
SEARCH_PAGES = 20
DOCUMENTS = ("outcomes", "outcomes-responses")
# What the page needs of a merged head's job to draw its round; the rest of a job row is left out.
HEAD_JOB_KEYS = ("id", "name", "status", "conclusion", "category", "created_at", "started_at", "completed_at")

MERGED = """
query($search: String!, $count: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  search(query: $search, type: ISSUE, first: $count, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        createdAt
        mergedAt
        headRefName
        headRefOid
        repository { nameWithOwner }
        headRepository { nameWithOwner }
        timelineItems(itemTypes: [READY_FOR_REVIEW_EVENT], last: 1) {
          nodes { ... on ReadyForReviewEvent { createdAt } }
        }
      }
    }
  }
}
"""


def merged_pull(node: object, owner: str) -> dict | None:
    """A search result as the reader uses it, or None when GitHub left out what it needs."""
    if not isinstance(node, dict):
        return None
    repository = (node.get("repository") or {}).get("nameWithOwner")
    head_repository = (node.get("headRepository") or {}).get("nameWithOwner")
    created, merged = parse_time(node.get("createdAt")), parse_time(node.get("mergedAt"))
    number, ref, sha = node.get("number"), node.get("headRefName"), node.get("headRefOid")
    if not (isinstance(repository, str) and isinstance(head_repository, str) and isinstance(number, int)
            and not isinstance(number, bool) and isinstance(ref, str) and isinstance(sha, str) and created and merged):
        return None
    ready = [parse_time((item or {}).get("createdAt")) for item in (node.get("timelineItems") or {}).get("nodes") or []]
    # A pull request opened ready for review has no ready event: it was ready from the start.
    ready_at = max([created, *filter(None, ready)])
    return {"repository": repository, "number": number, "title": str(node.get("title") or "Untitled pull request")[:300],
            "url": github_url(node.get("url"), owner), "created_at": iso_time(created), "merged_at": iso_time(merged),
            "ready_at": iso_time(min(ready_at, merged)), "head_ref": ref, "head_sha": sha,
            "head_repository": head_repository}


def search_page(payload: object, owner: str) -> tuple[list[dict], str | None]:
    """One page of the merged search: its pull requests and the cursor of the next page, if any."""
    result = ((payload.get("data") or {}) if isinstance(payload, dict) else {}).get("search")
    if not isinstance(result, dict) or not isinstance(result.get("nodes"), list):
        raise ApiError("invalid_response")
    page = result.get("pageInfo") or {}
    return [pull for pull in (merged_pull(node, owner) for node in result["nodes"]) if pull], (
        page.get("endCursor") if page.get("hasNextPage") else None)


def check_totals(runs: list[dict], jobs: dict) -> list[dict]:
    """Runner time per check over every attempt of every run: the time each job held a runner. A job a
    later attempt lists again (one a re-run of failed jobs kept) counts once."""
    totals: dict[str, dict] = {}
    seen = set()
    for run in runs:
        for attempt in range(1, (run.get("run_attempt") or 1) + 1):
            for job in jobs[(run["id"], attempt)]:
                seconds = job.get("elapsed_seconds")
                if not bot_job_ran(job) or not isinstance(seconds, int) or job.get("id") in seen:
                    continue
                seen.add(job.get("id"))
                name = "%s / %s" % (str(run.get("name") or "GitHub Actions")[:200], job["name"])
                row = totals.setdefault(name, {"name": name, "jobs": 0, "seconds": 0, "self_hosted_seconds": 0})
                row["jobs"] += 1
                row["seconds"] += seconds
                row["self_hosted_seconds"] += seconds if job.get("self_hosted") else 0
    return sorted(totals.values(), key=lambda row: row["name"])


def head_runs(runs: list[dict], sha: str, jobs: dict) -> list[dict]:
    """The merged head's runs, newest attempt of each and newest copy of each job, as the live view keeps them."""
    shaped = [{"id": run["id"], "suite_id": run.get("check_suite_id"), "path": run.get("path"),
               "name": str(run.get("name") or "GitHub Actions")[:200], "created_at": run.get("created_at"),
               "jobs": jobs[(run["id"], run.get("run_attempt") or 1)]}
              for run in _newest_attempts([run for run in runs if run.get("head_sha") == sha]).values()]
    _, kept = GitHubCollector._current_only([], shaped)
    return [{"id": run["id"], "name": run["name"], "jobs": [{key: job.get(key) for key in HEAD_JOB_KEYS} for job in run["jobs"]]}
            for run in kept]


def _valid_record(row: object) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("repository"), str) and isinstance(row.get("number"), int)
            and parse_time(row.get("merged_at")) is not None and isinstance(row.get("head_runs"), list)
            and isinstance(row.get("checks"), list))


class OutcomesReader:
    def __init__(self, config, api: GitHubAPI | None = None, *, live=None, clock=None):
        self.config = config
        self.api = api or GitHubAPI(max_calls=MAX_CALLS)
        # The live collector: its owner kind, its REST counter, and the job lists it kept of runs it saw finish.
        self.live = live
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.records: dict[tuple, dict] = {}
        self.pending = 0
        self.sampled_at: str | None = None
        self.error: str | None = None
        self._jobs: dict[tuple, list[dict]] = {}
        self._kind: str | None = None
        self._lock = threading.Lock()

    def _owner_kind(self) -> str:
        kind = self._kind or getattr(self.live, "_kind", None)
        if kind not in ("User", "Organization"):
            kind = owner_kind(self.api.graphql(OWNER, {"login": self.config.owner}))
            if kind is None:
                raise ApiError("invalid_response")
        self._kind = kind
        return kind

    def _merged(self, since: datetime) -> list[dict]:
        qualifier = "org" if self._owner_kind() == "Organization" else "user"
        search = "is:pr is:merged %s:%s merged:>=%s sort:updated-desc" % (qualifier, self.config.owner, since.strftime("%Y-%m-%d"))
        found, cursor = [], None
        for _ in range(SEARCH_PAGES):
            variables = {"search": search, "count": SEARCH_PAGE, **({"cursor": cursor} if cursor else {})}
            pulls, cursor = search_page(self.api.graphql(MERGED, variables), self.config.owner)
            found.extend(pulls)
            if not cursor:
                break
        return [pull for pull in found if parse_time(pull["merged_at"]) >= since]

    def _live_jobs(self, key: tuple) -> list[dict] | None:
        """A finished run's job list the live collector already read, when it carries the runner kind."""
        lock, kept = getattr(self.live, "_job_lock", None), getattr(self.live, "_completed_jobs", None)
        if lock is None or kept is None:
            return None
        with lock:
            rows = (kept.get(key) or (None, None))[1]
        if not rows or not all("self_hosted" in row for row in rows):
            return None
        return [{name: value for name, value in row.items() if name != "steps"} for row in rows]

    def _attempt_jobs(self, repository: str, run: dict, attempt: int) -> list[dict]:
        key = (repository, run["id"], attempt)
        rows = self._jobs.get(key) or self._live_jobs(key)
        if rows is None:
            endpoint = _endpoint("repos/%s/actions/runs/%s/attempts/%s/jobs" % (repository, run["id"], attempt), per_page=100)
            rows = [job_row(row, self.config.owner, self.clock()) for row in self.api.items(endpoint, "jobs")
                    if row.get("run_id") in (None, run["id"])]
        self._jobs[key] = rows
        return rows

    def _runs(self, pull: dict) -> list[dict]:
        """Every run on the pull request's branch while it was open, from its own repository."""
        endpoint = _endpoint("repos/%s/actions/runs" % pull["repository"], branch=pull["head_ref"],
                             created="%s..%s" % (pull["created_at"], pull["merged_at"]), per_page=100)
        rows = self.api.items(endpoint, "workflow_runs")
        return [row for row in rows if isinstance(row.get("id"), int)
                and str((row.get("head_repository") or {}).get("full_name") or "").casefold() == pull["head_repository"].casefold()]

    def _record(self, pull: dict) -> dict | None:
        runs = self._runs(pull)
        if any(run.get("status") != "completed" for run in runs):
            return None
        jobs = {(run["id"], attempt): self._attempt_jobs(pull["repository"], run, attempt)
                for run in runs for attempt in range(1, (run.get("run_attempt") or 1) + 1)}
        for run_id, attempt in jobs:
            self._jobs.pop((pull["repository"], run_id, attempt), None)
        record = {key: pull[key] for key in ("repository", "number", "title", "url", "created_at", "merged_at", "ready_at")}
        return {**record, "head_runs": head_runs(runs, pull["head_sha"], jobs), "checks": check_totals(runs, jobs)}

    def _reserve_low(self) -> bool:
        live = getattr(getattr(self.live, "api", None), "core_reserve_low", None)
        return self.api.core_reserve_low() or bool(callable(live) and live())

    def _read_new(self, merged: list[dict], records: dict) -> None:
        """Read each merged pull request without a record, newest first. A spent budget stops it."""
        for pull in sorted(merged, key=lambda pull: pull["merged_at"], reverse=True):
            key = (pull["repository"], pull["number"])
            if key not in records:
                record = self._record(pull)
                if record:
                    records[key] = record

    def collect(self, repositories) -> None:
        """One cycle: list the window's merged pull requests and read the ones without a record until
        the budget is spent. Raises ApiError only when access to the owner is lost."""
        if self._reserve_low():
            with self._lock:
                self.error = "github_budget_low"
            return
        now = self.clock()
        since = now - timedelta(days=OUTCOME_DAYS)
        allowed = {name.casefold() for name in repositories}
        with self._lock:
            records = {key: row for key, row in self.records.items() if parse_time(row["merged_at"]) >= since}
        merged, error = None, None
        try:
            self.api.begin()
            merged = [pull for pull in self._merged(since) if pull["repository"].casefold() in allowed]
            self._read_new(merged, records)
        except ApiError as failure:
            if failure.code in PRIVATE_FAILURES:
                raise
            error = None if failure.code == "request_budget_exhausted" else failure.code
        with self._lock:
            self.records = records
            if merged is not None:
                self.pending = sum((pull["repository"], pull["number"]) not in records for pull in merged)
            self.sampled_at, self.error = iso_time(now), error

    def view(self) -> dict:
        with self._lock:
            pulls = sorted(copy.deepcopy(list(self.records.values())), key=lambda row: row["merged_at"], reverse=True)
            return {"available": self.sampled_at is not None, "window_days": OUTCOME_DAYS, "sampled_at": self.sampled_at,
                    "pending": self.pending, "error": self.error, "pulls": pulls}

    def state(self) -> dict:
        with self._lock:
            records, kept = copy.deepcopy(list(self.records.values())), {"sampled_at": self.sampled_at, "pending": self.pending}
        return dict(zip(DOCUMENTS, ({"records": records, **kept}, self.api.export_cache())))

    def restore(self, store) -> bool:
        """Take back the records and answers state() wrote, and when they were read, so a restart
        serves them at once. True when any record came back."""
        kept = store.load(DOCUMENTS[0])
        kept = kept if isinstance(kept, dict) else {}
        rows = kept.get("records")
        found = {(row["repository"], row["number"]): row for row in (rows if isinstance(rows, list) else []) if _valid_record(row)}
        sampled, pending = kept.get("sampled_at"), kept.get("pending")
        with self._lock:
            self.records.update(found)
            if found and parse_time(sampled) is not None:
                self.sampled_at = iso_time(parse_time(sampled))
                self.pending = pending if isinstance(pending, int) and not isinstance(pending, bool) and pending >= 0 else 0
        self.api.import_cache(store.load(DOCUMENTS[1]))
        return bool(found)


class OutcomesFeed:
    """Runs the reader in the background, at most one cycle at a time and one every REFRESH_SECONDS,
    and keeps what it read in the state store. Lost access forgets everything, on disk too."""

    def __init__(self, config, *, live=None, store=None, monotonic=time.monotonic, clock=None, reader=None):
        self.config, self.live, self.store, self.monotonic, self.clock = config, live, store, monotonic, clock
        self.reader = reader or OutcomesReader(config, live=live, clock=clock)
        self._next = 0.0
        self._running = False
        self._generation = 0
        self._lock = threading.Lock()
        if store is not None:
            self.reader.restore(store)

    def reset(self) -> None:
        with self._lock:
            self._generation += 1
            self.reader = OutcomesReader(self.config, live=self.live, clock=self.clock)
            self._next = 0.0
        if self.store is not None:
            self.store.clear(*DOCUMENTS)

    def start(self, repositories) -> None:
        """Start a cycle in the background when one is due. None means the repositories are not known yet."""
        with self._lock:
            if repositories is None or self._running or self.monotonic() < self._next:
                return
            self._running = True
            reader, generation = self.reader, self._generation
        threading.Thread(target=self._cycle, args=(reader, generation, tuple(repositories)), daemon=True).start()

    def _cycle(self, reader, generation, repositories) -> None:
        lost = False
        try:
            reader.collect(repositories)
        except ApiError as failure:
            lost = failure.code in PRIVATE_FAILURES
        finally:
            with self._lock:
                self._running = False
                self._next = self.monotonic() + REFRESH_SECONDS
                current = generation == self._generation
        if lost and current:
            self.reset()
        elif current and self.store is not None:
            for name, document in reader.state().items():
                self.store.save(name, document)
            with self._lock:
                # Written outside the lock: access lost while it was writing leaves no outcomes file.
                if generation != self._generation:
                    self.store.clear(*DOCUMENTS)

    def view(self) -> dict:
        with self._lock:
            reader = self.reader
        return reader.view()
