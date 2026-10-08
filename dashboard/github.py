"""Current-head GitHub collection and normalized bot history."""

from __future__ import annotations

import base64
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from .config import BOT_KEYS, REPOSITORY, BotDefinition, Config, coverage_label
from .gh_api import ApiError, GitHubAPI
from .pull_signals import face_fields, load_signals
from .bot_runs import recent_bot_runs, runner_name
from .util import category, elapsed_seconds, github_url, iso_time, parse_time, status_category

MAX_WORKERS = 4
JOB_CACHE_AGE = timedelta(days=7)
WORKFLOW_CACHE_AGE = timedelta(minutes=5)
# History reads jobs only for this many of a workflow's newest completed runs. A configured job name
# that never runs would otherwise read every run of the last seven days, hundreds in a busy repository.
HISTORY_RUN_LIMIT = 50
# Shown on a pull request that was listed but whose checks did not fit in this pass's budget.
CHECKS_NOT_LOADED = "Checks are not loaded yet"
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)
_SEARCH_REPOSITORY = "https://api.github.com/repos/"


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


class GitHubCollector:
    def __init__(self, config: Config, api: GitHubAPI | None = None, *, clock=None):
        self.config = config
        self.api = api or GitHubAPI()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs: dict[tuple[str, int, int], list[dict]] = {}
        self._completed_jobs: dict[tuple[str, int, int], tuple[datetime, list[dict]]] = {}
        self._job_lock = threading.Lock()
        self._workflows: dict[str, tuple[datetime, set[str]]] = {}
        self._pins: dict[tuple, tuple[str, datetime]] = {}
        # Detail finished, and detail started. Never-finished repositories are read before ones
        # that finished, and one that was started but did not finish waits behind ones not started.
        self._detailed_at: dict[str, datetime] = {}
        self._attempted_at: dict[str, datetime] = {}
        self._listing: str | None = None
        self._kind: str | None = None

    def clear_private_cache(self) -> None:
        with self._job_lock:
            self._jobs.clear()
            self._completed_jobs.clear()
            self._workflows.clear()
            self._pins.clear()
        self._detailed_at.clear()
        self._attempted_at.clear()
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

        def newest(rows: list, key, rank) -> set[int]:
            latest = {}
            for index, row in enumerate(rows):
                if key(row) not in latest or rank(row) > latest[key(row)][0]:
                    latest[key(row)] = (rank(row), index)
            return {index for _, index in latest.values()}

        keep = newest(checks, lambda row: (row["provider"], workflow.get(row["suite_id"]), row["name"]),
                      lambda row: (created.get(row["suite_id"]) or _time_key(row, "started_at"), row["id"] or 0))
        checks = [row for index, row in enumerate(checks) if index in keep]
        jobs = [(run_index, job) for run_index, run in enumerate(runs) for job in run["jobs"]]
        keep = newest(jobs, lambda pair: (workflow[runs[pair[0]]["suite_id"]], pair[1]["name"]),
                      lambda pair: (created[runs[pair[0]]["suite_id"]], pair[1]["id"] or 0))
        current = []
        for run_index, run in enumerate(runs):
            run_jobs = [job for index, (owner, job) in enumerate(jobs) if owner == run_index and index in keep]
            if run_jobs or not run["jobs"]:
                current.append({**run, "jobs": run_jobs})
        return checks, current

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
        return {**identity, **face, "draft": draft, "head_changed": False, "evidence_available": True,
                "attention": attention, "attention_reason": reason,
                "checks": checks, "statuses": statuses, "expected": expected, "runs": runs}

    def _branch_rules(self, repository: str, base: str) -> list[dict]:
        from .required_checks import branch_rules
        return branch_rules(self.api, repository, base)

    def _unloaded_pull(self, repository: str, row: dict, inventory: dict, code: str,
                       signals: dict | None = None) -> dict:
        """A listed pull request whose checks were not read. A spent budget says so; a real access
        failure stays unavailable. Signals already read for this pull request are kept."""
        identity = self._pull_identity(repository, row, inventory)
        face = face_fields(None, signals, evidence=False, draft=identity["draft"], checks=[], statuses=[], expected=[])
        budget = code == "request_budget_exhausted"
        pull = {**identity, **face, "head_changed": False, "evidence_available": False, "attention": True,
                "attention_reason": CHECKS_NOT_LOADED if budget else "Current-head evidence is unavailable",
                "checks": [], "statuses": [], "expected": [], "runs": [], "source_error": code}
        if not budget:
            pull["unavailable"] = True
        return pull

    def _detail_repository(self, repository: str, rows: list[dict], inventory: dict) -> tuple[dict, bool]:
        """Read current-head detail. True when the budget stopped this repository before it finished."""
        pulls, errors, rules = [], [], {}
        signals = load_signals(self.api, repository, len(rows))
        stopped = False
        for row in rows:
            if stopped:
                pulls.append(self._unloaded_pull(repository, row, inventory, "request_budget_exhausted",
                                                 signals.get(row.get("number"))))
                errors.append({"pull": row.get("number"), "code": "request_budget_exhausted"})
                continue
            try:
                base = (row.get("base") or {}).get("ref")
                if isinstance(base, str) and base not in rules:
                    rules[base] = self._branch_rules(repository, base)
                pulls.append(self._pull(repository, row, inventory, rules.get(base, []),
                                        signals.get(row.get("number"))))
            except ApiError as error:
                pulls.append(self._unloaded_pull(repository, row, inventory, error.code,
                                                 signals.get(row.get("number"))))
                errors.append({"pull": row.get("number"), "code": error.code})
                stopped = error.code == "request_budget_exhausted"
        return {"repository": repository, **inventory, "pulls": pulls, "errors": errors}, stopped

    def _repository(self, repository: str, inventory: dict) -> dict:
        rows = self.api.items(_endpoint("repos/%s/pulls" % repository, state="open", per_page=100))
        return self._detail_repository(repository, rows, inventory)[0]

    def _by_need(self, names: list[str]) -> list[str]:
        """Never successfully detailed first (never started before started-but-unfinished), then
        the least recently detailed. A repository added since the last pass has neither stamp."""
        def key(name: str) -> tuple:
            success = self._detailed_at.get(name)
            if success is None:
                return (0, self._attempted_at.get(name) or _EARLIEST, names.index(name))
            return (1, success, names.index(name))
        return sorted(names, key=key)

    def _known_inventory(self, repository: str, inventory: dict[str, dict] | None) -> dict:
        known = (inventory or {}).get(repository)
        return known if known else self.inventory(repository)

    def _installation_rows(self) -> list[dict] | None:
        try:
            rows = self.api.items("installation/repositories", "repositories")
        except ApiError as error:
            if self._listing == "installation" or error.code not in ("forbidden", "not_found"):
                raise
            return None
        self._listing = "installation"
        return rows

    def _repository_rows(self) -> list[dict]:
        """Every repository this credential can see for the owner, one page at a time.

        An installation token reads `GET /installation/repositories` (user or organization).
        A person's `gh` login is refused there, and then reads `GET /orgs/{owner}/repos` or
        `GET /users/{owner}/repos?type=owner`. The choice is remembered for this process."""
        if self._listing != "account":
            rows = self._installation_rows()
            if rows is not None:
                return rows
        if self._kind is None:
            described = self.api.one("users/%s" % self.config.owner)
            kind, login = described.get("type"), described.get("login")
            if kind not in ("User", "Organization") or not isinstance(login, str) or login.casefold() != self.config.owner.casefold():
                raise ApiError("invalid_response")
            self._kind = kind
        endpoint = ("orgs/%s/repos" % self.config.owner if self._kind == "Organization"
                    else _endpoint("users/%s/repos" % self.config.owner, type="owner"))
        rows = self.api.items(endpoint)
        self._listing = "account"
        return rows

    def _names_from(self, rows: object) -> list[str]:
        if not isinstance(rows, list):
            raise ApiError("invalid_response")
        names, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ApiError("invalid_response")
            full = row.get("full_name")
            if not isinstance(full, str) or full.count("/") != 1:
                raise ApiError("invalid_response")
            repo_owner, name = full.split("/", 1)
            if repo_owner.casefold() != self.config.owner.casefold() or not REPOSITORY.fullmatch(name):
                raise ApiError("invalid_response")
            nested = row.get("owner")
            if isinstance(nested, dict) and isinstance(nested.get("login"), str) and nested["login"].casefold() != self.config.owner.casefold():
                raise ApiError("invalid_response")
            archived = row.get("archived")
            if archived is True:
                continue
            if archived is not False:
                raise ApiError("invalid_response")
            canonical = self.config.owner + "/" + name
            if canonical.casefold() in seen:
                continue
            seen.add(canonical.casefold())
            names.append(canonical)
        return names

    @staticmethod
    def _kind_from(rows: list[dict]) -> str | None:
        kinds = set()
        for row in rows:
            owner = row.get("owner")
            if isinstance(owner, dict) and owner.get("type") in ("User", "Organization"):
                kinds.add(owner["type"])
        if len(kinds) == 1:
            return kinds.pop()
        return None

    def _open_names(self, kind: str, names: list[str]) -> set[str]:
        """Which of names have an open pull request, from one owner-wide search.

        `GET /search/issues` with `org:` or `user:`. Repositories absent from it are not listed
        one by one. An incomplete search is a failure, not an empty owner."""
        query = "is:pr is:open %s:%s" % ("org" if kind == "Organization" else "user", self.config.owner)
        known = {name.casefold(): name for name in names}
        found: set[str] = set()
        for page in range(1, 11):
            payload = self.api.one(_endpoint("search/issues", q=query, per_page=100, page=page))
            if not isinstance(payload, dict) or payload.get("incomplete_results") is True:
                raise ApiError("unavailable")
            items = payload.get("items")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ApiError("invalid_response")
            for item in items:
                url = item.get("repository_url")
                if not isinstance(url, str) or not url.startswith(_SEARCH_REPOSITORY):
                    raise ApiError("invalid_response")
                match = known.get(url[len(_SEARCH_REPOSITORY):].casefold())
                if match:
                    found.add(match)
            if len(items) < 100:
                return found
        raise ApiError("unavailable")

    def _discover(self) -> tuple[tuple[str, ...], set[str]]:
        """Non-archived repositories of the owner, and which of them have an open pull request.

        A repository owned by anyone else refuses the whole discovery. Archived repositories
        are left out. The set is not cached: the next refresh sees a repository that appeared
        or disappeared."""
        rows = self._repository_rows()
        names = self._names_from(rows)
        if not names:
            return (), set()
        kind = self._kind_from(rows) or self._kind
        if kind is None:
            described = self.api.one("users/%s" % self.config.owner)
            kind, login = described.get("type"), described.get("login")
            if kind not in ("User", "Organization") or not isinstance(login, str) or login.casefold() != self.config.owner.casefold():
                raise ApiError("invalid_response")
            self._kind = kind
        return tuple(names), self._open_names(kind, names)

    def _bot_row(self, repository: str, role: str, run: dict, job: dict) -> dict:
        return {"bot": role, "repository": repository, "run_id": run.get("id"),
                "attempt": run.get("run_attempt") or 1, "job_id": job["id"], "name": job["name"],
                "status": job["status"], "conclusion": job["conclusion"], "category": job["category"],
                "started_at": job["started_at"], "completed_at": job["completed_at"],
                "elapsed_seconds": job["elapsed_seconds"], "html_url": job["html_url"],
                "runner_name": job.get("runner_name")}

    def _bot_workflow(self, repository: str, workflow: str, roles: list[str], now: datetime) -> dict:
        rows = {role: {"active": [], "completed": []} for role in roles}
        errors, active_complete, history_complete, floor = [], False, False, None
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
            for index, run in enumerate(runs):
                latest_five = [recent_bot_runs(rows[role]["completed"], now, 168) for role in roles]
                updated = parse_time(run.get("updated_at"))
                if (updated and updated < now - timedelta(hours=2)
                        and all(len(recent) >= 5 and updated <= parse_time(recent[-1]["completed_at"])
                                for recent in latest_five)):
                    break
                if index == HISTORY_RUN_LIMIT:
                    # This run and every later one finished no later than this run's update time;
                    # _bots decides per role whether its newest results make them irrelevant.
                    floor = _time_key(run, "updated_at", "created_at")
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
                "history_complete": history_complete, "history_floor": floor, "errors": errors,
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

    def _bots(self, now: datetime, readable_repositories=None, names=None) -> dict:
        groups: dict[tuple[str, str], list[str]] = {}
        all_rows = {role: {"active": [], "completed": [], "active_complete": True,
                           "history_complete": True, "active_at": [], "history_at": [],
                           "floors": []} for role in BOT_KEYS}
        errors = []
        repositories = self.config.repositories if names is None else names
        readable = set(repositories if readable_repositories is None else readable_repositories)
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
                for role, definition in self.config.bots.items():
                    if definition.workflow in workflow_names:
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
                    if outcome["history_floor"]:
                        value["floors"].append(outcome["history_floor"])
                    value["active_at"].append(outcome["active_at"])
                    value["history_at"].append(outcome["history_at"])
        result = {}
        for role, value in all_rows.items():
            active = sorted(value["active"], key=lambda row: _time_key(row, "started_at"), reverse=True)
            history = recent_bot_runs(value["completed"], now, 168)
            active_complete, history_complete = value["active_complete"], value["history_complete"]
            # A scan cut short by HISTORY_RUN_LIMIT is complete for this role only when its five newest
            # results, across every repository, are all newer than the runs that scan skipped.
            if value["floors"] and (len(history) < 5 or parse_time(history[-1]["completed_at"]) < max(value["floors"])):
                history_complete = False
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

    def _empty_repository(self, repository: str, inventory: dict, when: datetime) -> dict:
        return {"repository": repository, "subscription": inventory.get("subscription", "unknown"),
                "pulls": [], "errors": [], "sampled_at": iso_time(when),
                **({"evidence": inventory["evidence"]} if "evidence" in inventory else {})}

    def _not_loaded_repository(self, repository: str, rows: list[dict], inventory: dict) -> dict:
        known = inventory or {"subscription": "unknown"}
        return {"repository": repository, **known, "sampled_at": iso_time(self.clock()),
                "pulls": [self._unloaded_pull(repository, row, known, "request_budget_exhausted") for row in rows],
                "errors": [{"pull": row.get("number"), "code": "request_budget_exhausted"} for row in rows]}

    def _remember(self, names: tuple[str, ...] | list[str]) -> None:
        keep = set(names)
        self._detailed_at = {name: when for name, when in self._detailed_at.items() if name in keep}
        self._attempted_at = {name: when for name, when in self._attempted_at.items() if name in keep}

    def collect(self, inventory: dict[str, dict] | None = None) -> dict:
        """List every repository's open pull requests before any run or job detail.

        Detail is then spent on the repositories that have gone longest without it, so a busy
        head of the list cannot starve the tail on every pass. A pull request that was listed
        but not detailed stays visible with its checks not loaded yet. `repositories: all`
        learns the set, and which of it has an open pull request, before that, and does not
        read a repository the search says is empty."""
        now = self.clock().astimezone(timezone.utc)
        with self._job_lock:
            self._jobs = {}
        self._prune_job_cache(now)
        self.api.begin()
        rate = self.api.rate()
        if self.config.all_repositories:
            names, open_names = self._discover()
        else:
            names, open_names = self.config.repositories, set(self.config.repositories)
        self._remember(names)
        listings: dict[str, list[dict]] = {}
        errors: list[dict] = []
        budget_hit = False
        if self.config.all_repositories:
            for repository in names:
                if repository not in open_names:
                    listings[repository] = []
        to_list = [repository for repository in names if repository not in listings]
        for repository in self._by_need(to_list):
            if budget_hit:
                break
            try:
                listings[repository] = self.api.items(
                    _endpoint("repos/%s/pulls" % repository, state="open", per_page=100))
            except ApiError as error:
                errors.append({"repository": repository, "code": error.code})
                budget_hit = error.code == "request_budget_exhausted"
        for repository in names:
            if repository not in listings and not any(error.get("repository") == repository for error in errors):
                errors.append({"repository": repository, "code": "request_budget_exhausted"})
                budget_hit = True

        inventories: dict[str, dict] = {}
        by_repository: dict[str, dict] = {}
        for repository, rows in listings.items():
            if rows:
                continue
            # The open-pull list completed and was empty. An all-mode repository the search
            # left out costs nothing more. A listed repository still learns its subscription
            # when the budget has room, which is what a configured list did before.
            idle = self.config.all_repositories and repository not in open_names
            if idle or budget_hit:
                repo_inventory = {"subscription": "unknown"} if idle else (inventory or {}).get(repository) or {"subscription": "unknown"}
            else:
                try:
                    repo_inventory = self._known_inventory(repository, inventory)
                except ApiError as error:
                    if error.code != "request_budget_exhausted":
                        errors.append({"repository": repository, "code": error.code})
                        continue
                    budget_hit = True
                    repo_inventory = (inventory or {}).get(repository) or {"subscription": "unknown"}
                else:
                    inventories[repository] = repo_inventory
            if repository in inventories or idle or budget_hit:
                by_repository[repository] = self._empty_repository(repository, repo_inventory, now)
                self._detailed_at[repository] = self.clock()

        detail = self._by_need([repository for repository, rows in listings.items() if rows])
        for repository in detail:
            rows = listings[repository]
            known = (inventory or {}).get(repository) or {"subscription": "unknown"}
            if budget_hit or self.api.calls >= self.api.max_calls:
                # Not started. A previous successful detail stays the least-recent stamp, and this
                # repository is still never-attempted, so it is first next pass.
                by_repository[repository] = self._not_loaded_repository(repository, rows, known)
                budget_hit = True
                continue
            self._attempted_at[repository] = self.clock()
            try:
                repo_inventory = self._known_inventory(repository, inventory)
            except ApiError as error:
                errors.append({"repository": repository, "code": error.code})
                if error.code == "request_budget_exhausted":
                    budget_hit = True
                    by_repository[repository] = self._not_loaded_repository(repository, rows, known)
                    self._detailed_at.pop(repository, None)
                continue
            inventories[repository] = repo_inventory
            row, stopped = self._detail_repository(repository, rows, repo_inventory)
            row["sampled_at"] = iso_time(self.clock())
            by_repository[repository] = row
            if stopped:
                budget_hit = True
                self._detailed_at.pop(repository, None)
            else:
                self._detailed_at[repository] = self.clock()
        if budget_hit:
            errors.append({"code": "request_budget_exhausted"})
        bots = self._bots(self.clock(), by_repository, names)
        repository_rows = [by_repository[name] for name in names if name in by_repository]
        return {"owner": self.config.owner, "sampled_at": iso_time(now), "repositories": repository_rows,
                "repository_names": list(names),
                "coverage": {"selected": len(names), "readable": len(repository_rows),
                             "label": coverage_label(self.config), "inventory": inventories},
                "bots": bots, "errors": errors, "partial": bool(errors) or bots["partial"],
                "api": {**rate, "calls": self.api.calls, "lowest_remaining": self.api.lowest_remaining,
                        "max_calls": self.api.max_calls}}
