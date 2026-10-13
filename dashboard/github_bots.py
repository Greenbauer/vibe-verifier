"""Normalized bot history: each role's running jobs and recent results, read from GitHub Actions.

Kept out of github.py so that file stays within the length ratchet. Each function stays within
the complexity ratchet on its own: a new module may not contain one over the limit.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .bot_runs import bot_job_ran, pulls_bot_jobs, recent_bot_runs
from .config import BOT_KEYS
from .gh_api import ApiError
from .github import JOB_CACHE_AGE, MAX_WORKERS, _endpoint, _time_key
from .util import iso_time, parse_time

WORKFLOW_CACHE_AGE = timedelta(minutes=5)
# History reads jobs only for this many of a workflow's newest completed runs. A configured job name
# that never runs would otherwise read every run of the last seven days, hundreds in a busy repository.
HISTORY_RUN_LIMIT = 50


def _same_repository(run: dict, repository: str) -> bool:
    """A run reported for this repository; a run that names none is taken as this one."""
    return ((run.get("repository") or {}).get("full_name") or repository).lower() == repository.lower()


def _history_done(rows: dict, run: dict, now: datetime) -> bool:
    """No later run can change any role's five newest results: every role has five, this run was
    updated over two hours ago, and no later than each role's fifth."""
    latest_five = [recent_bot_runs(value["completed"], now, 168) for value in rows.values()]
    updated = parse_time(run.get("updated_at"))
    return bool(updated and updated < now - timedelta(hours=2)
                and all(len(recent) >= 5 and updated <= parse_time(recent[-1]["completed_at"])
                        for recent in latest_five))


def _sampled_at(times: list, current: object) -> str | None:
    """The oldest sample time, when the reading it dates is current."""
    return iso_time(min(times)) if times and current else None


def _role_result(value: dict, now: datetime) -> dict:
    """One role's state, history and coverage from the rows every repository gave it."""
    active = sorted(value["active"], key=lambda row: _time_key(row, "started_at"), reverse=True)
    history = recent_bot_runs(value["completed"], now, 168)
    active_complete, history_complete = value["active_complete"], value["history_complete"]
    # A scan cut short by HISTORY_RUN_LIMIT is complete for this role only when its five newest
    # results, across every repository, are all newer than the runs that scan skipped.
    if value["floors"] and (len(history) < 5 or parse_time(history[-1]["completed_at"]) < max(value["floors"])):
        history_complete = False
    state = "working" if active else "idle" if active_complete else "unknown"
    return {"state": state, "state_source": "github_actions" if state != "unknown" else "unavailable",
            "sampled_at": _sampled_at(value["active_at"], active_complete or active),
            "history_sampled_at": _sampled_at(value["history_at"], history_complete or history),
            "active": active, "recent_2h": recent_bot_runs(value["completed"], now, 2),
            "recent_7d": history,
            "latest_failure": history[0] if history and history[0]["category"] == "failed" else None,
            "coverage": {"active": "complete" if active_complete else "unavailable",
                         "history": "complete" if history_complete else "partial"}}


class BotHistory:
    def _bot_row(self, repository: str, role: str, run: dict, job: dict) -> dict:
        return {"bot": role, "repository": repository, "run_id": run.get("id"),
                "attempt": run.get("run_attempt") or run.get("attempt") or 1, "job_id": job["id"], "name": job["name"],
                "status": job["status"], "conclusion": job["conclusion"], "category": job["category"],
                "started_at": job["started_at"], "completed_at": job["completed_at"],
                "elapsed_seconds": job["elapsed_seconds"], "html_url": job["html_url"],
                "runner_name": job.get("runner_name")}

    def _add_bot_job(self, rows: dict, phase: str, role_jobs: dict, repository: str, run: dict, job: dict) -> None:
        """Add the job under phase for every role that names it."""
        for role, names in role_jobs.items():
            if job["name"] in names:
                rows[role][phase].append(self._bot_row(repository, role, run, job))

    def _active_bot_jobs(self, repository: str, base: str, role_jobs: dict, rows: dict) -> None:
        """Running bot jobs of the workflow's in-progress runs. Raises ApiError."""
        active_runs = self.api.items(_endpoint(base, status="in_progress", per_page=100), "workflow_runs")
        for run in active_runs:
            if not _same_repository(run, repository):
                continue
            for job in self._run_jobs(repository, run):
                if job["status"] == "in_progress" and bot_job_ran(job):
                    self._add_bot_job(rows, "active", role_jobs, repository, run, job)

    def _completed_bot_jobs(self, repository: str, base: str, role_jobs: dict, rows: dict,
                            now: datetime) -> datetime | None:
        """Completed bot jobs of the last seven days, newest runs first. Returns the history floor
        when HISTORY_RUN_LIMIT ended the scan, else None. Raises ApiError."""
        cutoff = now - JOB_CACHE_AGE
        endpoint = _endpoint(base, status="completed", created=">=" + cutoff.date().isoformat(), per_page=100)
        # GitHub pages are not a completion-time ordering. Gather lightweight run
        # metadata before sorting, so a recent rerun on a later page is not skipped.
        runs = [run for page, _ in self.api.page_items(endpoint, "workflow_runs") for run in page
                if _same_repository(run, repository) and _time_key(run, "created_at") >= cutoff]
        runs.sort(key=lambda run: _time_key(run, "updated_at", "created_at"), reverse=True)
        for index, run in enumerate(runs):
            if _history_done(rows, run, now):
                break
            if index == HISTORY_RUN_LIMIT:
                # This run and every later one finished no later than this run's update time;
                # _bots decides per role whether its newest results make them irrelevant.
                return _time_key(run, "updated_at", "created_at")
            for job in self._run_jobs(repository, run):
                if job["status"] == "completed" and job.get("completed_at") and bot_job_ran(job):
                    self._add_bot_job(rows, "completed", role_jobs, repository, run, job)
        return None

    def _bot_workflow(self, repository: str, workflow: str, roles: list[str], now: datetime, base: str) -> dict:
        rows = {role: {"active": [], "completed": []} for role in roles}
        errors, active_complete, history_complete, floor = [], False, False, None
        role_jobs = {role: set(self.config.bots[role].jobs) for role in roles}
        try:
            self._active_bot_jobs(repository, base, role_jobs, rows)
            active_complete = True
        except ApiError as error:
            errors.append({"repository": repository, "workflow": workflow,
                           "phase": "active", "code": self._runs_error(repository, workflow, base, error)})

        active_at = self.clock()
        try:
            floor = self._completed_bot_jobs(repository, base, role_jobs, rows, now)
            history_complete = True
        except ApiError as error:
            errors.append({"repository": repository, "workflow": workflow,
                           "phase": "history", "code": self._runs_error(repository, workflow, base, error)})
        return {"rows": rows, "active_complete": active_complete,
                "history_complete": history_complete, "history_floor": floor, "errors": errors,
                "active_at": active_at, "history_at": self.clock()}

    def _runs_error(self, repository: str, workflow: str, base: str, error: ApiError) -> str:
        if not self._required.forget(repository, workflow, error.code, base):
            return error.code
        with self._job_lock:
            self._workflows.pop(repository, None)
        return "unavailable"

    def _workflow_names(self, repository: str) -> set[str]:
        now = self.clock()
        cached = self._workflows.get(repository)
        if cached and timedelta(0) <= now - cached[0] < WORKFLOW_CACHE_AGE:
            return cached[1]
        rows = self.api.items(_endpoint("repos/%s/actions/workflows" % repository, per_page=100), "workflows")
        names = {row["path"].rsplit("/", 1)[-1] for row in rows if isinstance(row.get("path"), str)}
        self._workflows[repository] = (self.clock(), names)
        return names

    def _group_roles(self, repository: str, workflow_names: set[str], groups: dict, all_rows: dict) -> None:
        """Group each role under the runs endpoint of its workflow, when the workflow is present."""
        for role, definition in self.config.bots.items():
            base = self._required.runs(repository, definition.workflow, workflow_names)
            if base:
                groups.setdefault((repository, definition.workflow, base), []).append(role)
            else:
                # A successful owner-repo listing proves this workflow absent;
                # a workflow endpoint 404 alone cannot distinguish revoked access.
                all_rows[role]["active_at"].append(self.clock())
                all_rows[role]["history_at"].append(self.clock())

    def _bot_groups(self, repositories, readable: set, all_rows: dict, errors: list) -> dict:
        """Roles sharing each workflow scan, keyed by (repository, workflow, runs endpoint). A
        repository whose workflows cannot be listed leaves every role incomplete."""
        groups: dict[tuple[str, str, str], list[str]] = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {repository: pool.submit(self._workflow_names, repository)
                       for repository in repositories if repository in readable}
            for repository in repositories:
                try:
                    if repository not in futures:
                        raise ApiError("unavailable")
                    workflow_names = futures[repository].result()
                except ApiError as error:
                    errors.append({"repository": repository, "phase": "workflows", "code": error.code})
                    for value in all_rows.values():
                        value["active_complete"] = value["history_complete"] = False
                    continue
                self._required.search_or_mark(self.api, repository, self.config.bots, workflow_names, errors, all_rows)
                self._group_roles(repository, workflow_names, groups, all_rows)
        return groups

    def _scan_bot_groups(self, groups: dict, now: datetime, all_rows: dict, errors: list) -> None:
        """Scan each workflow once and add its outcome to every role that shares it."""
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {key: pool.submit(self._bot_workflow, *key[:2], roles, now, key[2])
                       for key, roles in groups.items()}
            for key, future in futures.items():
                outcome = future.result()
                errors.extend(outcome["errors"])
                for role in groups[key]:
                    value = all_rows[role]
                    value["active"].extend(outcome["rows"][role]["active"])
                    value["completed"].extend(outcome["rows"][role]["completed"])
                    value["active_complete"] &= outcome["active_complete"]
                    value["history_complete"] &= outcome["history_complete"]
                    if outcome["history_floor"]:
                        value["floors"].append(outcome["history_floor"])
                    value["active_at"].append(outcome["active_at"])
                    value["history_at"].append(outcome["history_at"])

    def _bots(self, now: datetime, readable_repositories=None, names=None) -> dict:
        all_rows = {role: {"active": [], "completed": [], "active_complete": True,
                           "history_complete": True, "active_at": [], "history_at": [],
                           "floors": []} for role in BOT_KEYS}
        errors = []
        repositories = self.config.repositories if names is None else names
        readable = set(repositories if readable_repositories is None else readable_repositories)
        groups = self._bot_groups(repositories, readable, all_rows, errors)
        self._scan_bot_groups(groups, now, all_rows, errors)
        self._take_pull_jobs(readable_repositories, all_rows)
        result = {role: _role_result(value, now) for role, value in all_rows.items()}
        return {"partial": bool(errors), "roles": result, "errors": errors}

    def _take_pull_jobs(self, payloads: object, all_rows: dict) -> None:
        """Required-workflow jobs already fetched on open pulls. Adds no API calls."""
        seen = {(row["repository"], row["job_id"])
                for value in all_rows.values() for row in value["active"] + value["completed"]}
        for repository, role, run, job in pulls_bot_jobs(payloads, self.config.bots):
            identity = (repository, job.get("id"))
            if identity in seen or role not in all_rows:
                continue
            seen.add(identity)
            row = self._bot_row(repository, role, run, job)
            bucket = all_rows[role]
            if job.get("status") == "in_progress":
                bucket["active"].append(row)
            elif job.get("status") == "completed" and job.get("completed_at"):
                bucket["completed"].append(row)
