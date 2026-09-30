"""Current-head GitHub collection and normalized bot history."""

from __future__ import annotations

import base64
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from .config import BOT_KEYS, BotDefinition, Config
from .gh_api import ApiError, GitHubAPI
from .util import category, elapsed_seconds, github_url, iso_time, parse_time, status_category

MAX_WORKERS = 4
JOB_CACHE_AGE = timedelta(days=7)
WORKFLOW_CACHE_AGE = timedelta(minutes=5)


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
    complete = [row for row in rows if row.get("completed_at")
                and cutoff <= parse_time(row["completed_at"]) <= now]
    complete.sort(key=lambda row: parse_time(row["completed_at"]), reverse=True)
    return complete[:limit]


class GitHubCollector:
    def __init__(self, config: Config, api: GitHubAPI | None = None, *, clock=None):
        self.config = config
        self.api = api or GitHubAPI()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs: dict[tuple[str, int, int], list[dict]] = {}
        self._completed_jobs: dict[tuple[str, int, int], tuple[datetime, list[dict]]] = {}
        self._job_lock = threading.Lock()
        self._workflows: dict[str, tuple[datetime, set[str]]] = {}

    def clear_private_cache(self) -> None:
        with self._job_lock:
            self._jobs.clear()
            self._completed_jobs.clear()
            self._workflows.clear()

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
        latest, suite_ids = {}, set()
        for suite in suites:
            suite_id = suite.get("id")
            if not isinstance(suite_id, int) or suite.get("head_sha") not in (None, sha):
                continue
            suite_ids.add(suite_id)
            rows = self.api.items(_endpoint("repos/%s/check-suites/%s/check-runs" % (repository, suite_id),
                                            per_page=100, filter="latest"), "check_runs")
            for row in rows:
                if ((row.get("check_suite") or {}).get("id") not in (None, suite_id)
                        or row.get("head_sha") not in (None, sha)):
                    continue
                key = self._check_identity(row, suite_id)
                rank = (_time_key(row, "started_at", "completed_at"), row.get("id") or 0)
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
                         "runner_id": row.get("runner_id"), "steps": self._clean_steps(row.get("steps"))})
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

    @staticmethod
    def _attention(evidence: list[dict], runs: list[dict]) -> tuple[bool, str]:
        categories = [row["category"] for row in evidence]
        if "failed" in categories:
            return True, "A current-head check failed"
        if "cancelled" in categories:
            return True, "A current-head check was cancelled"
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

    def _pull(self, repository: str, row: dict, inventory: dict) -> dict:
        identity = self._pull_identity(repository, row, inventory)
        number, sha = identity["number"], identity["head_sha"]
        if not isinstance(number, int) or not isinstance(sha, str):
            raise ApiError("invalid_response")
        checks, suites = self._check_runs(repository, sha)
        statuses = self._statuses(repository, sha)
        runs = self._actions_runs(repository, sha, suites)
        current = self.api.one("repos/%s/pulls/%s" % (repository, number))
        if (current.get("head") or {}).get("sha") != sha:
            return {**identity, "head_sha": (current.get("head") or {}).get("sha"), "head_changed": True,
                    "evidence_available": False, "attention": True,
                    "attention_reason": "Head changed while GitHub evidence was loading",
                    "checks": [], "statuses": [], "runs": []}
        attention, reason = self._attention(checks + statuses, runs)
        return {**identity, "head_changed": False, "evidence_available": True,
                "attention": attention, "attention_reason": reason,
                "checks": checks, "statuses": statuses, "runs": runs}

    def _repository(self, repository: str, inventory: dict) -> dict:
        rows = self.api.items(_endpoint("repos/%s/pulls" % repository, state="open", per_page=100))
        pulls, errors = [], []
        for row in rows:
            try:
                pulls.append(self._pull(repository, row, inventory))
            except ApiError as error:
                identity = self._pull_identity(repository, row, inventory)
                pulls.append({**identity, "head_changed": False, "evidence_available": False,
                              "unavailable": True, "attention": True,
                              "attention_reason": "Current-head evidence is unavailable",
                              "checks": [], "statuses": [], "runs": [], "source_error": error.code})
                errors.append({"pull": row.get("number"), "code": error.code})
        return {"repository": repository, **inventory, "pulls": pulls, "errors": errors}

    def _bot_row(self, repository: str, role: str, run: dict, job: dict) -> dict:
        return {"bot": role, "repository": repository, "run_id": run.get("id"),
                "attempt": run.get("run_attempt") or 1, "job_id": job["id"], "name": job["name"],
                "status": job["status"], "conclusion": job["conclusion"], "category": job["category"],
                "started_at": job["started_at"], "completed_at": job["completed_at"],
                "elapsed_seconds": job["elapsed_seconds"], "html_url": job["html_url"]}

    def _bot_workflow(self, repository: str, workflow: str, roles: list[str], now: datetime) -> dict:
        rows = {role: {"active": [], "completed": []} for role in roles}
        errors, active_complete, history_complete = [], False, False
        role_jobs = {role: set(self.config.bots[role].jobs) for role in roles}
        base = "repos/%s/actions/workflows/%s/runs" % (repository, quote(workflow, safe=""))
        try:
            active_runs = self.api.items(_endpoint(base, status="in_progress", per_page=100), "workflow_runs")
            for run in active_runs:
                if ((run.get("repository") or {}).get("full_name") or repository).lower() != repository.lower():
                    continue
                for job in self._run_jobs(repository, run):
                    if job["status"] != "in_progress":
                        continue
                    for role in roles:
                        if job["name"] in role_jobs[role]:
                            rows[role]["active"].append(self._bot_row(repository, role, run, job))
            active_complete = True
        except ApiError as error:
            errors.append({"repository": repository, "workflow": workflow,
                           "phase": "active", "code": error.code})

        active_at = self.clock()
        cutoff = now - JOB_CACHE_AGE
        try:
            endpoint = _endpoint(base, status="completed", created=">=" + cutoff.date().isoformat(), per_page=100)
            # GitHub pages are not a completion-time ordering. Gather lightweight run
            # metadata before sorting, so a recent rerun on a later page is not skipped.
            runs = [run for page, _ in self.api.page_items(endpoint, "workflow_runs") for run in page
                    if ((run.get("repository") or {}).get("full_name") or repository).lower() == repository.lower()
                    and _time_key(run, "created_at") >= cutoff]
            runs.sort(key=lambda run: _time_key(run, "updated_at", "created_at"), reverse=True)
            for run in runs:
                latest_five = [recent_bot_runs(rows[role]["completed"], now, 168) for role in roles]
                updated = parse_time(run.get("updated_at"))
                if (updated and updated < now - timedelta(hours=2)
                        and all(len(recent) >= 5 and updated <= parse_time(recent[-1]["completed_at"])
                                for recent in latest_five)):
                    break
                for job in self._run_jobs(repository, run):
                    if job["status"] != "completed" or not job.get("completed_at"):
                        continue
                    for role in roles:
                        if job["name"] in role_jobs[role]:
                            rows[role]["completed"].append(self._bot_row(repository, role, run, job))
            history_complete = True
        except ApiError as error:
            errors.append({"repository": repository, "workflow": workflow,
                           "phase": "history", "code": error.code})
        return {"rows": rows, "active_complete": active_complete,
                "history_complete": history_complete, "errors": errors,
                "active_at": active_at, "history_at": self.clock()}

    def _workflow_names(self, repository: str) -> set[str]:
        now = self.clock()
        cached = self._workflows.get(repository)
        if cached and timedelta(0) <= now - cached[0] < WORKFLOW_CACHE_AGE:
            return cached[1]
        rows = self.api.items(_endpoint("repos/%s/actions/workflows" % repository, per_page=100), "workflows")
        names = {row["path"].rsplit("/", 1)[-1] for row in rows if isinstance(row.get("path"), str)}
        self._workflows[repository] = (self.clock(), names)
        return names

    def _bots(self, now: datetime, readable_repositories=None) -> dict:
        groups: dict[tuple[str, str], list[str]] = {}
        all_rows = {role: {"active": [], "completed": [], "active_complete": True,
                           "history_complete": True, "active_at": [], "history_at": []} for role in BOT_KEYS}
        errors = []
        readable = set(self.config.repositories if readable_repositories is None else readable_repositories)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {repository: pool.submit(self._workflow_names, repository)
                       for repository in self.config.repositories if repository in readable}
            for repository in self.config.repositories:
                try:
                    if repository not in futures:
                        raise ApiError("unavailable")
                    names = futures[repository].result()
                except ApiError as error:
                    errors.append({"repository": repository, "phase": "workflows", "code": error.code})
                    for value in all_rows.values():
                        value["active_complete"] = value["history_complete"] = False
                    continue
                for role, definition in self.config.bots.items():
                    if definition.workflow in names:
                        groups.setdefault((repository, definition.workflow), []).append(role)
                    else:
                        # A successful owner-repo listing proves this workflow absent;
                        # a workflow endpoint 404 alone cannot distinguish revoked access.
                        all_rows[role]["active_at"].append(self.clock())
                        all_rows[role]["history_at"].append(self.clock())
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {(repository, workflow): pool.submit(self._bot_workflow, repository, workflow, roles, now)
                       for (repository, workflow), roles in groups.items()}
            for key, future in futures.items():
                outcome = future.result()
                errors.extend(outcome["errors"])
                for role in groups[key]:
                    value = all_rows[role]
                    value["active"].extend(outcome["rows"][role]["active"])
                    value["completed"].extend(outcome["rows"][role]["completed"])
                    value["active_complete"] &= outcome["active_complete"]
                    value["history_complete"] &= outcome["history_complete"]
                    value["active_at"].append(outcome["active_at"])
                    value["history_at"].append(outcome["history_at"])
        result = {}
        for role, value in all_rows.items():
            active = sorted(value["active"], key=lambda row: _time_key(row, "started_at"), reverse=True)
            history = recent_bot_runs(value["completed"], now, 168)
            active_complete, history_complete = value["active_complete"], value["history_complete"]
            state = "working" if active else "idle" if active_complete else "unknown"
            result[role] = {"state": state, "state_source": "github_actions" if state != "unknown" else "unavailable",
                            "sampled_at": iso_time(min(value["active_at"])) if value["active_at"] and (active_complete or active) else None,
                            "history_sampled_at": iso_time(min(value["history_at"])) if value["history_at"] and (history_complete or history) else None,
                            "active": active, "recent_2h": recent_bot_runs(value["completed"], now, 2),
                            "recent_7d": history,
                            "latest_failure": history[0] if history and history[0]["category"] == "failed" else None,
                            "coverage": {"active": "complete" if active_complete else "unavailable",
                                         "history": "complete" if history_complete else "partial"}}
        return {"partial": bool(errors), "roles": result, "errors": errors}

    def _collect_repository(self, repository: str, inventory: dict[str, dict] | None) -> tuple:
        known_inventory = (inventory or {}).get(repository)
        try:
            repo_inventory = known_inventory or self.inventory(repository)
            row = self._repository(repository, repo_inventory)
            row["sampled_at"] = iso_time(self.clock())
            return repository, repo_inventory, row, None
        except ApiError as error:
            return repository, known_inventory, None, error.code

    def collect(self, inventory: dict[str, dict] | None = None) -> dict:
        now = self.clock().astimezone(timezone.utc)
        with self._job_lock:
            self._jobs = {}
        self._prune_job_cache(now)
        self.api.begin()
        rate = self.api.rate()
        inventories, by_repository, errors = {}, {}, []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(self.config.repositories))) as pool:
            futures = [pool.submit(self._collect_repository, repository, inventory)
                       for repository in self.config.repositories]
            for future in futures:
                repository, repo_inventory, row, code = future.result()
                if repo_inventory:
                    inventories[repository] = repo_inventory
                if row:
                    by_repository[repository] = row
                if code:
                    errors.append({"repository": repository, "code": code})
        bots = self._bots(self.clock(), by_repository)
        repository_rows = [by_repository[name] for name in self.config.repositories if name in by_repository]
        return {"owner": self.config.owner, "sampled_at": iso_time(now), "repositories": repository_rows,
                "coverage": {"selected": len(self.config.repositories), "readable": len(repository_rows),
                             "label": "Selected repositories", "inventory": inventories},
                "bots": bots, "errors": errors, "partial": bool(errors) or bots["partial"],
                "api": {**rate, "calls": self.api.calls, "max_calls": self.api.max_calls}}
