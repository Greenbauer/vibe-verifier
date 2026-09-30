"""Current-head GitHub collection and normalization for the local dashboard."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from .config import BOT_KEYS, BotDefinition, Config
from .gh_api import ApiError, GitHubAPI
from .util import category, elapsed_seconds, github_url, iso_time, parse_time, status_category


def _endpoint(path: str, **query: object) -> str:
    values = {name: str(value) for name, value in query.items() if value is not None}
    return path + ("?" + urlencode(values) if values else "")


def _time_key(row: dict, *keys: str) -> datetime:
    for key in keys:
        value = parse_time(row.get(key))
        if value:
            return value
    return datetime.min.replace(tzinfo=timezone.utc)


def step_summary(jobs: list[dict]) -> dict:
    counts = {name: 0 for name in ("success", "failed", "skipped", "cancelled", "pending", "unknown")}
    total = completed = 0
    known = bool(jobs)
    for job in jobs:
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            known = False
            continue
        for step in steps:
            state = step["category"]
            counts[state] += 1
            total += 1
            if step.get("status") == "completed":
                completed += 1
    return {"known": known, "completed": completed if known else None, "total": total if known else None,
            "remaining": total - completed if known else None, "counts": counts,
            "percent": round(completed * 100 / total) if known and total else None}


def recent_bot_runs(rows: list[dict], now: datetime, hours: int, limit: int = 5) -> list[dict]:
    cutoff = now - timedelta(hours=hours)
    complete = [row for row in rows if row.get("completed_at") and cutoff <= parse_time(row["completed_at"]) <= now]
    complete.sort(key=lambda row: parse_time(row["completed_at"]), reverse=True)
    return complete[:limit]


class GitHubCollector:
    def __init__(self, config: Config, api: GitHubAPI | None = None, *, clock=None):
        self.config = config
        self.api = api or GitHubAPI()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs: dict[tuple[str, int, int], list[dict]] = {}

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
        """Direct evidence reuses the catalog's manifest/stub rule; absent direct evidence stays unknown."""
        manifest = self._content_exists(repository, ".vibe-verifier")
        stub = self._content_exists(repository, ".github/workflows/vibe-verifier.yml")
        if manifest or stub:
            return {"subscription": "subscribed", "evidence": "direct_manifest_or_workflow"}
        return {"subscription": "unknown", "evidence": "wrapper_or_ruleset_not_resolved"}

    def _check_runs(self, repository: str, sha: str) -> tuple[list[dict], set[int]]:
        suites = self.api.items(_endpoint("repos/%s/commits/%s/check-suites" % (repository, sha), per_page=100),
                                "check_suites")
        checks, suite_ids = [], set()
        for suite in suites:
            suite_id = suite.get("id")
            if not isinstance(suite_id, int) or suite.get("head_sha") not in (None, sha):
                continue
            suite_ids.add(suite_id)
            rows = self.api.items(_endpoint("repos/%s/check-suites/%s/check-runs" % (repository, suite_id),
                                            per_page=100, filter="all"), "check_runs")
            for row in rows:
                if (row.get("check_suite") or {}).get("id") not in (None, suite_id):
                    continue
                checks.append({"id": row.get("id"), "suite_id": suite_id, "name": str(row.get("name") or "Unnamed check")[:200],
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
        key = (repository, run_id, attempt)
        if key in self._jobs:
            return self._jobs[key]
        if not isinstance(run_id, int) or not isinstance(attempt, int):
            return []
        rows = self.api.items(_endpoint("repos/%s/actions/runs/%s/attempts/%s/jobs" %
                                        (repository, run_id, attempt), per_page=100), "jobs")
        jobs = []
        for row in rows:
            if row.get("run_id") not in (None, run_id) or row.get("head_sha") not in (None, run.get("head_sha")):
                continue
            steps = self._clean_steps(row.get("steps"))
            jobs.append({"id": row.get("id"), "name": str(row.get("name") or "Unnamed job")[:200],
                         "status": row.get("status"), "conclusion": row.get("conclusion"),
                         "category": category(row.get("status"), row.get("conclusion")),
                         "created_at": row.get("created_at"), "started_at": row.get("started_at"),
                         "completed_at": row.get("completed_at"),
                         "queue_seconds": elapsed_seconds(row.get("created_at"), row.get("started_at"), self.clock()),
                         "elapsed_seconds": elapsed_seconds(row.get("started_at"), row.get("completed_at"), self.clock()),
                         "html_url": github_url(row.get("html_url"), self.config.owner), "steps": steps})
        self._jobs[key] = jobs
        return jobs

    def _actions_runs(self, repository: str, sha: str, suite_ids: set[int]) -> list[dict]:
        rows = self.api.items(_endpoint("repos/%s/actions/runs" % repository, head_sha=sha, per_page=100), "workflow_runs")
        candidates = [row for row in rows if row.get("head_sha") == sha and row.get("check_suite_id") in suite_ids
                      and ((row.get("repository") or {}).get("full_name") or repository).lower() == repository.lower()]
        newest = {}
        for row in candidates:
            run_id, attempt = row.get("id"), row.get("run_attempt") or 1
            if isinstance(run_id, int) and (run_id not in newest or attempt > (newest[run_id].get("run_attempt") or 1)):
                newest[run_id] = row
        result = []
        for row in newest.values():
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
                           "step_summary": step_summary(jobs)})
        return sorted(result, key=lambda row: _time_key(row, "started_at", "created_at"), reverse=True)

    def _attention(self, evidence: list[dict], runs: list[dict]) -> tuple[bool, str]:
        categories = [row["category"] for row in evidence]
        if "failed" in categories:
            return True, "A current-head check failed"
        if "cancelled" in categories:
            return True, "A current-head check was cancelled"
        for run in runs:
            for job in run["jobs"]:
                if job["status"] == "waiting" or job["conclusion"] == "action_required":
                    return True, "GitHub reports a job waiting for action"
        if "unknown" in categories:
            return True, "A current-head check has an unknown result"
        return False, "Current-head work is still running" if "pending" in categories else "No current-head failure observed"

    def _pull(self, repository: str, row: dict, inventory: dict) -> dict:
        number, sha = row.get("number"), (row.get("head") or {}).get("sha")
        if not isinstance(number, int) or not isinstance(sha, str):
            raise ApiError("invalid_response")
        checks, suites = self._check_runs(repository, sha)
        statuses = self._statuses(repository, sha)
        runs = self._actions_runs(repository, sha, suites)
        current = self.api.one("repos/%s/pulls/%s" % (repository, number))
        if (current.get("head") or {}).get("sha") != sha:
            return {"repository": repository, "number": number, "title": str(row.get("title") or "Untitled pull request")[:300],
                    "head_sha": (current.get("head") or {}).get("sha"), "head_changed": True,
                    "subscription": inventory["subscription"], "attention": True,
                    "attention_reason": "Head changed while GitHub evidence was loading", "checks": [], "statuses": [], "runs": []}
        attention, reason = self._attention(checks + statuses, runs)
        return {"repository": repository, "number": number, "title": str(row.get("title") or "Untitled pull request")[:300],
                "author": str(((row.get("user") or {}).get("login") or "unknown"))[:100], "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"), "age_seconds": elapsed_seconds(row.get("created_at"), None, self.clock()),
                "head_sha": sha, "head_changed": False, "draft": bool(row.get("draft")),
                "html_url": github_url(row.get("html_url"), self.config.owner),
                "subscription": inventory["subscription"], "attention": attention, "attention_reason": reason,
                "checks": checks, "statuses": statuses, "runs": runs}

    def _repository(self, repository: str, inventory: dict) -> dict:
        rows = self.api.items(_endpoint("repos/%s/pulls" % repository, state="open", per_page=100))
        pulls, errors = [], []
        for row in rows:
            try:
                pulls.append(self._pull(repository, row, inventory))
            except ApiError as error:
                errors.append({"pull": row.get("number"), "code": error.code})
        return {"repository": repository, **inventory, "pulls": pulls, "errors": errors}

    def _bot_rows(self, repository: str, role: str, definition: BotDefinition, cutoff: datetime) -> list[dict]:
        endpoint = _endpoint("repos/%s/actions/workflows/%s/runs" %
                             (repository, quote(definition.workflow, safe="")), per_page=100,
                             created=">=" + cutoff.date().isoformat())
        runs = self.api.items(endpoint, "workflow_runs")
        runs = [row for row in runs if ((row.get("repository") or {}).get("full_name") or repository).lower() == repository.lower()
                and _time_key(row, "created_at") >= cutoff]
        runs.sort(key=lambda row: _time_key(row, "updated_at", "created_at"), reverse=True)
        result = []
        for run in runs[:20]:
            for job in self._run_jobs(repository, run):
                if job["name"] not in definition.jobs:
                    continue
                result.append({"bot": role, "repository": repository, "run_id": run.get("id"), "attempt": run.get("run_attempt") or 1,
                               "job_id": job["id"], "name": job["name"], "status": job["status"],
                               "conclusion": job["conclusion"], "category": job["category"],
                               "started_at": job["started_at"], "completed_at": job["completed_at"],
                               "elapsed_seconds": job["elapsed_seconds"], "html_url": job["html_url"]})
        return result

    def _bots(self, now: datetime) -> dict:
        cutoff, all_rows, partial = now - timedelta(days=7), {role: [] for role in BOT_KEYS}, False
        for role, definition in self.config.bots.items():
            for repository in self.config.repositories:
                try:
                    all_rows[role].extend(self._bot_rows(repository, role, definition, cutoff))
                except ApiError:
                    partial = True
        result = {}
        for role, rows in all_rows.items():
            active = sorted((row for row in rows if row["category"] == "pending"),
                            key=lambda row: _time_key(row, "started_at"), reverse=True)
            history = recent_bot_runs(rows, now, 168)
            result[role] = {"state": "working" if active else "unknown", "active": active[:1],
                            "recent_2h": recent_bot_runs(rows, now, 2), "recent_7d": history,
                            "latest_failure": next((row for row in history if row["category"] == "failed"), None)}
        return {"partial": partial, "roles": result}

    def collect(self, inventory: dict[str, dict] | None = None) -> dict:
        now = self.clock().astimezone(timezone.utc)
        self._jobs = {}
        self.api.begin()
        rate = self.api.rate()
        inventories, repository_rows, errors = {}, [], []
        for repository in self.config.repositories:
            try:
                inventories[repository] = (inventory or {}).get(repository) or self.inventory(repository)
                repository_rows.append(self._repository(repository, inventories[repository]))
            except ApiError as error:
                errors.append({"repository": repository, "code": error.code})
        bots = self._bots(now)
        return {"owner": self.config.owner, "sampled_at": iso_time(now), "repositories": repository_rows,
                "coverage": {"selected": len(self.config.repositories), "readable": len(repository_rows),
                             "label": "Selected repositories", "inventory": inventories},
                "bots": bots, "errors": errors, "partial": bool(errors) or bots["partial"],
                "api": {**rate, "calls": self.api.calls, "max_calls": self.api.max_calls}}
