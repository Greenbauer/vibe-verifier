"""Current-head GitHub collection and normalized bot history."""

from __future__ import annotations

import base64
import copy
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .config import Config
from .gh_api import ApiError, GitHubAPI
from .merge_grant import MergeGrants
from .pull_signals import face_fields
from .required_workflows import RequiredWorkflows
from .bot_runs import runner_name
from .util import category, elapsed_seconds, github_url, iso_time, parse_time, status_category

MAX_WORKERS = 4
JOB_CACHE_AGE = timedelta(days=7)
# Shown on a pull request that was listed but whose checks did not fit in this pass's budget.
CHECKS_NOT_LOADED = "Checks are not loaded yet"


def _endpoint(path: str, **query: object) -> str:
    values = {name: str(value) for name, value in query.items() if value is not None}
    return path + ("?" + urlencode(values) if values else "")


def _time_key(row: dict, *keys: str) -> datetime:
    for key in keys:
        value = parse_time(row.get(key))
        if value:
            return value
    return datetime.min.replace(tzinfo=timezone.utc)


# The start of a pin whose ruleset history GitHub refused: every run counts as at the current pin.
PIN_UNKNOWN = datetime.min.replace(tzinfo=timezone.utc)


def _newest_attempts(rows: list[dict]) -> dict[int, dict]:
    """The newest attempt of each workflow run, by run id."""
    newest = {}
    for row in rows:
        run_id, attempt = row.get("id"), row.get("run_attempt") or 1
        if isinstance(run_id, int) and (run_id not in newest or attempt > (newest[run_id].get("run_attempt") or 1)):
            newest[run_id] = row
    return newest


def _newest(rows: list, key, rank) -> set[int]:
    """Indexes of the highest-ranked row for each key; the first wins a tie."""
    latest = {}
    for index, row in enumerate(rows):
        if key(row) not in latest or rank(row) > latest[key(row)][0]:
            latest[key(row)] = (rank(row), index)
    return {index for _, index in latest.values()}


def _kept_runs(runs: list[dict], jobs: list[tuple], keep: set[int]) -> list[dict]:
    """Each run with only its kept jobs. A run left with none is dropped, unless it never had any."""
    current = []
    for run_index, run in enumerate(runs):
        run_jobs = [job for index, (owner, job) in enumerate(jobs) if owner == run_index and index in keep]
        if run_jobs or not run["jobs"]:
            current.append({**run, "jobs": run_jobs})
    return current


# Imported after _endpoint: owner_set and github_bots read helpers from this module while they load.
from .github_bots import BotHistory
from .owner_set import OwnerSet


class GitHubCollector(OwnerSet, BotHistory):
    def __init__(self, config: Config, api: GitHubAPI | None = None, *, clock=None):
        self.config = config
        self.api = api or GitHubAPI()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs: dict[tuple[str, int, int], list[dict]] = {}
        self._completed_jobs: dict[tuple[str, int, int], tuple[datetime, list[dict]]] = {}
        self._job_lock = threading.Lock()
        self._workflows: dict[str, tuple[datetime, set[str]]] = {}
        self._required = RequiredWorkflows()
        self._pins: dict[tuple, tuple[str, datetime]] = {}
        # Detail finished, and detail started. Never-finished repositories are read before ones
        # that finished, and one that was started but did not finish waits behind ones not started.
        self._detailed_at: dict[str, datetime] = {}
        self._attempted_at: dict[str, datetime] = {}
        self._listing: str | None = None
        self._kind: str | None = None
        # Last detailed pull, and pull requests whose head or rollup has since changed.
        self._detail: dict[tuple, dict] = {}
        self._urgent: set[tuple] = set()
        self._heads: dict = {}
        self._heads_ok = False
        self._heads_complete = False
        self._read_now: set[tuple] = set()
        # Which grant of the merge policy covers each pull request's current head (dashboard/merge_grant.py).
        self._grants = MergeGrants(config.merge_policy) if config.merge_policy else None

    def clear_private_cache(self) -> None:
        with self._job_lock:
            self._jobs.clear()
            self._completed_jobs.clear()
            self._workflows.clear()
            self._pins.clear()
        self._required.clear()
        self._detailed_at.clear()
        self._attempted_at.clear()
        self._detail.clear()
        self._urgent.clear()
        self._heads = {}
        self._grants = MergeGrants(self.config.merge_policy) if self.config.merge_policy else None
        self._heads_ok = False
        self._heads_complete = False
        self._read_now = set()
        self._listing = None
        self._kind = None
        self.api.clear_cache()

    def _prune_job_cache(self, now: datetime) -> None:
        with self._job_lock:
            self._completed_jobs = {key: value for key, value in self._completed_jobs.items()
                                    if value[0] >= now}

    def _content_exists(self, repository: str, path: str) -> bool:
        try:
            value = self.api.one("repos/%s/contents/%s" % (repository, path))
            content = value.get("content")
            if isinstance(content, str):
                base64.b64decode(content, validate=False)
            return value.get("type") == "file"
        except ApiError as error:
            if error.code == "not_found":
                return False
            raise

    def inventory(self, repository: str) -> dict:
        manifest = self._content_exists(repository, ".vibe-verifier")
        stub = self._content_exists(repository, ".github/workflows/vibe-verifier.yml")
        if manifest or stub:
            return {"subscription": "subscribed", "evidence": "direct_manifest_or_workflow"}
        return {"subscription": "unknown", "evidence": "wrapper_or_ruleset_not_resolved"}

    @staticmethod
    def _check_identity(row: dict, suite_id: int) -> tuple:
        app = row.get("app") or {}
        app_identity = app.get("id") or app.get("slug") or app.get("name") or "unknown"
        return suite_id, str(app_identity), str(row.get("name") or "Unnamed check")

    def _check_runs(self, repository: str, sha: str) -> tuple[list[dict], set[int]]:
        suites = self.api.items(_endpoint("repos/%s/commits/%s/check-suites" % (repository, sha), per_page=100),
                                "check_suites")
        suite_ids = {suite.get("id") for suite in suites
                     if isinstance(suite.get("id"), int) and suite.get("head_sha") in (None, sha)}
        # filter=all, one listing for the head. filter=latest keeps the newest completed_at and drops
        # a queued rerun; the newest check run id is that attempt, which often has no timestamps.
        rows = self.api.items(_endpoint("repos/%s/commits/%s/check-runs" % (repository, sha),
                                        per_page=100, filter="all"), "check_runs")
        latest = {}
        for row in rows:
            suite_id = (row.get("check_suite") or {}).get("id")
            if suite_id not in suite_ids or row.get("head_sha") not in (None, sha):
                continue
            key = self._check_identity(row, suite_id)
            rank = (row.get("id") or 0, _time_key(row, "started_at", "completed_at"))
            if key not in latest or rank > latest[key][0]:
                latest[key] = (rank, row, suite_id)
        checks = []
        for _, row, suite_id in latest.values():
            checks.append({"id": row.get("id"), "suite_id": suite_id,
                           "name": str(row.get("name") or "Unnamed check")[:200],
                           "provider": str(((row.get("app") or {}).get("name") or "GitHub check"))[:120],
                           "status": row.get("status"), "conclusion": row.get("conclusion"),
                           "category": category(row.get("status"), row.get("conclusion")),
                           "started_at": row.get("started_at"), "completed_at": row.get("completed_at"),
                           "elapsed_seconds": elapsed_seconds(row.get("started_at"), row.get("completed_at"), self.clock()),
                           "details_url": github_url(row.get("details_url"), self.config.owner)})
        return checks, suite_ids

    def _statuses(self, repository: str, sha: str) -> list[dict]:
        value = self.api.one("repos/%s/commits/%s/status" % (repository, sha))
        rows = value.get("statuses")
        if not isinstance(rows, list):
            raise ApiError("invalid_response")
        return [{"id": row.get("id"), "name": str(row.get("context") or "Commit status")[:200],
                 "provider": "Commit status", "status": row.get("state"), "conclusion": row.get("state"),
                 "category": status_category(row.get("state")), "started_at": row.get("created_at"),
                 "completed_at": row.get("updated_at"),
                 "elapsed_seconds": elapsed_seconds(row.get("created_at"), row.get("updated_at"), self.clock()),
                 "details_url": github_url(row.get("target_url"), self.config.owner)}
                for row in rows if isinstance(row, dict)]

    def _clean_steps(self, steps: object) -> list[dict] | None:
        if not isinstance(steps, list):
            return None
        return [{"number": row.get("number"), "name": str(row.get("name") or "Unnamed step")[:200],
                 "status": row.get("status"), "conclusion": row.get("conclusion"),
                 "category": category(row.get("status"), row.get("conclusion")),
                 "started_at": row.get("started_at"), "completed_at": row.get("completed_at"),
                 "elapsed_seconds": elapsed_seconds(row.get("started_at"), row.get("completed_at"), self.clock())}
                for row in steps if isinstance(row, dict)]

    def _run_jobs(self, repository: str, run: dict) -> list[dict]:
        run_id, attempt = run.get("id"), run.get("run_attempt") or 1
        if not isinstance(run_id, int) or not isinstance(attempt, int):
            return []
        key = (repository, run_id, attempt)
        with self._job_lock:
            if key in self._jobs:
                return copy.deepcopy(self._jobs[key])
            cached = self._completed_jobs.get(key)
            if cached:
                self._jobs[key] = cached[1]
                return copy.deepcopy(cached[1])
        rows = self.api.items(_endpoint("repos/%s/actions/runs/%s/attempts/%s/jobs" %
                                        (repository, run_id, attempt), per_page=100), "jobs")
        jobs = []
        for row in rows:
            if row.get("run_id") not in (None, run_id) or row.get("head_sha") not in (None, run.get("head_sha")):
                continue
            jobs.append({"id": row.get("id"), "name": str(row.get("name") or "Unnamed job")[:200],
                         "status": row.get("status"), "conclusion": row.get("conclusion"),
                         "category": category(row.get("status"), row.get("conclusion")),
                         "created_at": row.get("created_at"), "started_at": row.get("started_at"),
                         "completed_at": row.get("completed_at"),
                         "queue_seconds": elapsed_seconds(row.get("created_at"), row.get("started_at"), self.clock()),
                         "elapsed_seconds": elapsed_seconds(row.get("started_at"), row.get("completed_at"), self.clock()),
                         "html_url": github_url(row.get("html_url"), self.config.owner),
                         "runner_id": row.get("runner_id"), "runner_name": runner_name(row.get("runner_name")),
                         "steps": self._clean_steps(row.get("steps"))})
        with self._job_lock:
            self._jobs[key] = jobs
            completed_at = _time_key(run, "updated_at")
            if (run.get("status") == "completed" and jobs
                    and all(job["status"] == "completed" for job in jobs)
                    and completed_at > datetime.min.replace(tzinfo=timezone.utc)):
                self._completed_jobs[key] = (completed_at + JOB_CACHE_AGE, jobs)
        return copy.deepcopy(jobs)

    def _actions_runs(self, repository: str, sha: str, suite_ids: set[int]) -> list[dict]:
        rows = self.api.items(_endpoint("repos/%s/actions/runs" % repository, head_sha=sha, per_page=100),
                              "workflow_runs")
        self._required.learn(repository, rows)
        candidates = [row for row in rows if row.get("head_sha") == sha and row.get("check_suite_id") in suite_ids
                      and ((row.get("repository") or {}).get("full_name") or repository).lower() == repository.lower()]
        result = []
        for row in _newest_attempts(candidates).values():
            jobs = self._run_jobs(repository, row)
            result.append({"id": row.get("id"), "suite_id": row.get("check_suite_id"),
                           "name": str(row.get("name") or "GitHub Actions")[:200], "path": row.get("path"),
                           "attempt": row.get("run_attempt") or 1, "status": row.get("status"),
                           "conclusion": row.get("conclusion"),
                           "category": category(row.get("status"), row.get("conclusion")),
                           "created_at": row.get("created_at"), "started_at": row.get("run_started_at"),
                           "completed_at": row.get("updated_at") if row.get("status") == "completed" else None,
                           "elapsed_seconds": elapsed_seconds(row.get("run_started_at"),
                                                              row.get("updated_at") if row.get("status") == "completed" else None,
                                                              self.clock()),
                           "html_url": github_url(row.get("html_url"), self.config.owner), "jobs": jobs,
                           "required_workflow": "/actions/required_workflows/" in str(row.get("workflow_url") or "")})
        return sorted(result, key=lambda row: _time_key(row, "started_at", "created_at"), reverse=True)

    def _expected(self, repository: str, rules: list[dict], evidence: list[dict], runs: list[dict]) -> list[dict]:
        from .required_checks import expected_rows
        return expected_rows(self, repository, rules, evidence, runs)

    @staticmethod
    def _current_only(checks: list[dict], runs: list[dict]) -> tuple[list[dict], list[dict]]:
        """Keep only the newest copy of each job and check, as GitHub's pull request page does.

        Each event that starts a workflow on the same head (a push, a review, a comment) creates a new
        run and check suite, so one job can appear several times. Older copies are superseded."""
        created = {run["suite_id"]: _time_key(run, "created_at") for run in runs}
        workflow = {run["suite_id"]: run["path"] or run["name"] for run in runs}
        keep = _newest(checks, lambda row: (row["provider"], workflow.get(row["suite_id"]), row["name"]),
                       lambda row: (created.get(row["suite_id"]) or _time_key(row, "started_at"), row["id"] or 0))
        checks = [row for index, row in enumerate(checks) if index in keep]
        jobs = [(run_index, job) for run_index, run in enumerate(runs) for job in run["jobs"]]
        keep = _newest(jobs, lambda pair: (workflow[runs[pair[0]]["suite_id"]], pair[1]["name"]),
                       lambda pair: (created[runs[pair[0]]["suite_id"]], pair[1]["id"] or 0))
        return checks, _kept_runs(runs, jobs, keep)

    @staticmethod
    def _attention(evidence: list[dict], runs: list[dict], expected: list[dict]) -> tuple[bool, str]:
        categories = [row["category"] for row in evidence]
        if "failed" in categories:
            return True, "A current-head check failed"
        if "cancelled" in categories:
            return True, "A current-head check was cancelled"
        if expected:
            return True, "%d required check%s not run on this head" % (len(expected), "" if len(expected) == 1 else "s")
        if any(job["status"] == "waiting" or job["conclusion"] == "action_required"
               for run in runs for job in run["jobs"]):
            return True, "GitHub reports a job waiting for action"
        if "unknown" in categories:
            return True, "A current-head check has an unknown result"
        return False, "Current-head work is still running" if "pending" in categories else "No current-head failure observed"

    def _pull_identity(self, repository: str, row: dict, inventory: dict) -> dict:
        return {"repository": repository, "number": row.get("number"),
                "title": str(row.get("title") or "Untitled pull request")[:300],
                "author": str(((row.get("user") or {}).get("login") or "unknown"))[:100],
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                "age_seconds": elapsed_seconds(row.get("created_at"), None, self.clock()),
                "head_sha": (row.get("head") or {}).get("sha"), "draft": bool(row.get("draft")),
                "html_url": github_url(row.get("html_url"), self.config.owner),
                "subscription": inventory["subscription"]}

    def _pull(self, repository: str, row: dict, inventory: dict, rules: list[dict] = (),
              signals: dict | None = None) -> dict:
        identity = self._pull_identity(repository, row, inventory)
        number, sha = identity["number"], identity["head_sha"]
        if not isinstance(number, int) or not isinstance(sha, str):
            raise ApiError("invalid_response")
        checks, suites = self._check_runs(repository, sha)
        statuses = self._statuses(repository, sha)
        runs = self._actions_runs(repository, sha, suites)
        current = self.api.one("repos/%s/pulls/%s" % (repository, number))
        draft = current.get("draft") if isinstance(current.get("draft"), bool) else identity["draft"]
        if (current.get("head") or {}).get("sha") != sha:
            face = face_fields(current, signals, evidence=False, draft=draft, checks=[], statuses=[], expected=[])
            return {**identity, **face, "head_sha": (current.get("head") or {}).get("sha"), "head_changed": True,
                    "evidence_available": False, "attention": True,
                    "attention_reason": "Head changed while GitHub evidence was loading",
                    "checks": [], "statuses": [], "expected": [], "runs": []}
        checks, runs = self._current_only(checks, runs)
        expected = self._expected(repository, rules, checks + statuses, runs)
        attention, reason = self._attention(checks + statuses, runs, expected)
        face = face_fields(current, signals, evidence=True, draft=draft, checks=checks, statuses=statuses,
                           expected=expected, rules=rules)
        grant = self._grants.grant(self.api, repository, current) if self._grants else None
        return {**identity, **face, "draft": draft, "head_changed": False, "evidence_available": True, "merge_grant": grant,
                "attention": attention, "attention_reason": reason, "checks_sampled_at": iso_time(self.clock()),
                "checks": checks, "statuses": statuses, "expected": expected, "runs": runs}

    def _branch_rules(self, repository: str, base: str) -> list[dict]:
        from .required_checks import branch_rules
        return branch_rules(self.api, repository, base)
