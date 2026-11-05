"""A small GitHub behind a fake `gh`, for tests that run the real collector end to end.

It answers `gh api --include <endpoint>` (and GraphQL) the way the GitHub CLI does: a 2xx prints
the status line, headers and body and exits 0; anything else exits 1 with `gh: ... (HTTP n)` on
stderr. REST answers carry an ETag, and a matching If-None-Match gets a 304. The repository has one
open pull request on `main` whose head has two Actions runs (one a ruleset-required workflow) and
an external check, plus `history` completed runs of each bot workflow over the past week and
`suites` check suites on the head. `refuse` makes chosen endpoint families fail, and `siblings`
are more repositories of the same owner. Every read is in `log`.

The collector's own modules are never imported here, so the same fake serves the collector of any
commit (the proof that a check fails on a past bug runs it against old `dashboard/` code)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import parse_qsl, urlsplit

from dashboard_fake_graphql import answer as graphql_answer, graphql_cost

OWNER = "octocat"
REPO = OWNER + "/example"
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
RESET = int((NOW + timedelta(hours=1)).timestamp())
GATE = {"path": ".github/workflows/gate.yml", "repository_id": 900, "ref": "refs/heads/main", "sha": "c" * 40}
BOT_WORKFLOWS = {"review.yml": "review", "explore.yml": "explore"}
JOB_NAMES = {"ci.yml": "test", "gate.yml": "gate", **BOT_WORKFLOWS}

# How each refusal reads on the wire: (status, stderr, extra headers).
REFUSALS = {
    "forbidden": (403, "gh: Resource not accessible by integration (HTTP 403)", {}),
    "plan": (403, "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)", {}),
    "spent": (403, "gh: API rate limit exceeded for installation. (HTTP 403)",
              {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(RESET)}),
    "unavailable": (502, "gh: Server Error (HTTP 502)", {}),
}


class Read(NamedTuple):
    family: str
    endpoint: str
    status: int  # 304 for an unchanged answer to If-None-Match
    points: int = 0  # GraphQL only: what GitHub charges for the query


def family(endpoint: str) -> str:
    """The endpoint's path with owner, repository, numbers, SHAs and workflow files as `*`."""
    parts = urlsplit(endpoint).path.strip("/").split("/")
    named = {"repos": 3, "orgs": 2, "users": 2}.get(parts[0], 1)
    return "/".join("*" if 0 < index < named or part.isdigit() or re.fullmatch(r"[0-9a-f]{40}", part)
                    or part.endswith(".yml") else part for index, part in enumerate(parts))


class FakeGitHub:
    owner, now, sha, rollup = OWNER, NOW, "a" * 40, "SUCCESS"

    def __init__(self, repo: str = REPO, *, suites: int = 1, history: int = 5, closed_pulls: int = 0,
                 siblings: tuple = ()):
        self.repo, self.suites, self.history, self.closed_pulls = repo, suites, history, closed_pulls
        self.siblings = list(siblings)
        self.refuse: dict[str, str] = {}
        self.log: list[Read] = []
        self._runs = self.runs()

    @staticmethod
    def iso(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- the repository: subclasses change these ---------------------------------------------
    def run(self, run_id, path, sha, created, *, conclusion="success", suite=None, required=False):
        url = "https://api.github.com/repos/%s/actions/" % self.repo
        return {"id": run_id, "run_attempt": 1, "check_suite_id": suite or run_id + 1000, "head_sha": sha,
                "name": path[:-4].capitalize(), "path": ".github/workflows/" + path, "event": "pull_request",
                "status": "completed", "conclusion": conclusion, "created_at": self.iso(created),
                "run_started_at": self.iso(created + timedelta(seconds=10)),
                "updated_at": self.iso(created + timedelta(minutes=4)),
                "html_url": "https://github.com/%s/actions/runs/%d" % (self.repo, run_id),
                "workflow_url": url + ("required_workflows/5" if required else "workflows/" + path),
                "repository": {"full_name": self.repo}, "head_repository": {"full_name": self.repo},
                "pull_requests": [{"number": 3}]}

    def runs(self) -> list[dict]:
        rows = [self.run(11, "ci.yml", self.sha, NOW - timedelta(minutes=30), suite=7),
                self.run(12, "gate.yml", self.sha, NOW - timedelta(minutes=30), suite=6, required=True)]
        for offset, path in enumerate(BOT_WORKFLOWS):
            for index in range(self.history):
                created = NOW - timedelta(minutes=40 + offset * 7) - index * timedelta(days=6) / max(self.history, 1)
                rows.append(self.run(100000 + offset * 10000 + index, path, "b" * 40, created))
        return rows

    def check_run(self, check_id, name, suite, *, app=None, conclusion="success", minutes=29) -> dict:
        actions = {"id": 15368, "name": "GitHub Actions", "slug": "github-actions"}
        return {"id": check_id, "name": name, "status": "completed", "conclusion": conclusion, "head_sha": self.sha,
                "started_at": self.iso(NOW - timedelta(minutes=minutes)),
                "completed_at": self.iso(NOW - timedelta(minutes=minutes - 2)), "check_suite": {"id": suite},
                "details_url": "https://github.com/%s/runs/%d" % (self.repo, check_id), "app": app or actions}

    def check_runs(self, query: dict) -> list[dict]:
        return [self.check_run(91, "External analysis", 8, app={"id": 40, "name": "Example checks"}),
                self.check_run(92, "test", 7), self.check_run(93, "gate", 6)]

    def rules(self) -> list[dict]:
        return [{"type": "required_status_checks", "ruleset_source_type": "Repository", "ruleset_id": 76,
                 "ruleset_source": self.repo, "parameters": {"required_status_checks": [{"context": "test"}]}},
                {"type": "workflows", "ruleset_source_type": "Organization", "ruleset_source": OWNER,
                 "ruleset_id": 77, "parameters": {"workflows": [GATE]}}]

    def protection(self) -> tuple[int, object]:
        return 404, {"message": "Branch not protected"}

    # -- answers --------------------------------------------------------------------------------
    def _job(self, run: dict) -> dict:
        started = run["run_started_at"]
        return {"id": run["id"] * 10, "run_id": run["id"], "run_attempt": 1, "head_sha": run["head_sha"],
                "name": JOB_NAMES[run["path"].rsplit("/", 1)[-1]], "status": "completed", "conclusion": run["conclusion"],
                "created_at": run["created_at"], "started_at": started, "completed_at": run["updated_at"],
                "html_url": run["html_url"] + "/job/%d" % (run["id"] * 10), "runner_name": "lane-1",
                "workflow_name": run["name"], "labels": ["self-hosted"],
                "steps": [{"number": 1, "name": "Run", "status": "completed", "conclusion": run["conclusion"],
                           "started_at": started, "completed_at": run["updated_at"]}]}

    def _pull(self, number=3, state="open"):
        return {"number": number, "state": state, "title": "Fix the thing", "draft": False,
                "user": {"login": OWNER}, "created_at": self.iso(NOW - timedelta(days=1)),
                "updated_at": self.iso(NOW - timedelta(minutes=20)),
                "html_url": "https://github.com/%s/pull/%d" % (self.repo, number),
                "head": {"sha": self.sha, "ref": "fix/thing", "repo": {"full_name": self.repo}},
                "base": {"ref": "main", "repo": {"full_name": self.repo}},
                "mergeable": True, "mergeable_state": "clean", "comments": 0, "review_comments": 0}

    def _suites(self) -> dict:
        ids = sorted({row["check_suite"]["id"] for row in self.check_runs({"filter": "all"})})
        rows = [{"id": suite, "head_sha": self.sha} for suite in ids]
        rows += [{"id": 9000 + index, "head_sha": self.sha, "status": "completed", "conclusion": "skipped"}
                 for index in range(max(self.suites - len(ids), 0))]
        return {"total_count": len(rows), "check_suites": rows}

    def _workflow_runs(self, query: dict, path: str | None) -> dict:
        since = query.get("created", "").lstrip(">=")
        rows = [run for run in self._runs
                if (path is None or run["path"].endswith("/" + path))
                and query.get("head_sha", run["head_sha"]) == run["head_sha"]
                and query.get("status", run["status"]) == run["status"] and run["created_at"][:10] >= since[:10]]
        return {"total_count": len(rows), "workflow_runs": rows}

    def _owner_answer(self, path: str) -> tuple[int, object] | None:
        if path == "rate_limit":
            return 200, {"resources": {"core": {"remaining": 4000, "limit": 5000, "reset": RESET},
                                       "graphql": {"remaining": 4900, "limit": 5000, "reset": RESET}}}
        if path in ("users/" + OWNER, "orgs/" + OWNER):
            return (200, {"login": OWNER, "type": "User"}) if path.startswith("users") else (404, {})
        if path in ("users/%s/repos" % OWNER, "installation/repositories"):
            rows = [{"full_name": fake.repo, "name": fake.repo.split("/")[1], "owner": {"login": OWNER},
                     "archived": False, "private": False} for fake in [self, *self.siblings]]
            return 200, (rows if path.startswith("users") else {"total_count": len(rows), "repositories": rows})
        if path == "orgs/%s/rulesets/77/history" % OWNER:
            return 200, [{"version_id": 2, "updated_at": self.iso(NOW - timedelta(days=2))}]
        if path == "orgs/%s/rulesets/77/history/2" % OWNER:
            return 200, {"version_id": 2, "state": {"rules": [{"type": "workflows", "parameters": {"workflows": [GATE]}}]}}
        return None

    # One route per kind of repository read: (pattern after `repos/<owner>/<name>/`, answer).
    def _pulls(self, match, query):
        closed = [self._pull(100 + index, "closed") for index in range(self.closed_pulls)]
        return 200, [self._pull()] if query.get("state", "open") == "open" else closed

    def _check_listing(self, match, query):
        suite = match.group("suite")
        rows = [row for row in self.check_runs(query) if not suite or row["check_suite"]["id"] == int(suite)]
        return 200, {"total_count": len(rows), "check_runs": rows}

    def _jobs(self, match, query):
        jobs = [self._job(run) for run in self._runs if run["id"] == int(match.group(1))]
        return 200, {"total_count": len(jobs), "jobs": jobs}

    def _workflows(self, match, query):
        rows = [{"id": 500 + index, "name": path[:-4], "path": ".github/workflows/" + path, "state": "active"}
                for index, path in enumerate(["ci.yml", *BOT_WORKFLOWS])]
        return 200, {"total_count": len(rows), "workflows": rows}

    def _workflow_file_runs(self, match, query):
        name = match.group(1)
        files = {"500": "ci.yml", "501": "review.yml", "502": "explore.yml"}
        return 200, self._workflow_runs(query, name if name.endswith(".yml") else files.get(name))

    def _protection_read(self, match, query):
        status, body = self.protection()
        if status == 200 and match.group(1):
            body = body["required_status_checks"]
        return status, body

    ROUTES = (
        (r"pulls", _pulls),
        (r"pulls/3", lambda self, match, query: (200, self._pull())),
        (r"(?:commits/[0-9a-f]{40}|check-suites/(?P<suite>\d+))/check-runs", _check_listing),
        (r"commits/[0-9a-f]{40}/check-suites", lambda self, match, query: (200, self._suites())),
        (r"commits/[0-9a-f]{40}/status", lambda self, match, query: (200, {"state": "success", "statuses": []})),
        (r"actions/runs", lambda self, match, query: (200, self._workflow_runs(query, None))),
        (r"actions/runs/(\d+)(?:/attempts/\d+)?/jobs", _jobs),
        (r"actions/workflows", _workflows),
        (r"actions/workflows/([\w.-]+)/runs", _workflow_file_runs),
        (r"actions/required_workflows", lambda self, match, query: (200, {"total_count": 0, "required_workflows": []})),
        (r"rules/branches/main", lambda self, match, query: (200, self.rules())),
        (r"branches/main/protection(/required_status_checks)?", _protection_read),
        (r"actions/artifacts", lambda self, match, query: (200, {"total_count": 0, "artifacts": []})),
    )

    def _answer(self, endpoint: str) -> tuple[int, object]:
        parts = urlsplit(endpoint)
        path, query = parts.path.strip("/"), dict(parse_qsl(parts.query))
        owner = [fake for fake in self.siblings if path.startswith("repos/%s/" % fake.repo)]
        if owner:
            return owner[0]._answer(endpoint)
        prefix = "repos/%s/" % self.repo
        if not path.startswith(prefix):
            return self._owner_answer(path) or (404, {"message": "Not Found"})
        for pattern, route in self.ROUTES:
            match = re.fullmatch(pattern, path[len(prefix):])
            if match:
                return route(self, match, query)
        return 404, {"message": "Not Found"}

    # -- the CLI ------------------------------------------------------------------------------
    def _respond(self, status: int, body: object, headers: dict[str, str], stderr: str = "") -> subprocess.CompletedProcess:
        ok = 200 <= status < 300
        if not self._include:  # before --include, gh printed only the body of a 2xx
            return subprocess.CompletedProcess([], 0 if ok else 1, json.dumps(body) if ok else "",
                                               "" if ok else (stderr or "gh: HTTP %d" % status))
        head = {"content-type": "application/json", "x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4000",
                "x-ratelimit-reset": str(RESET), "x-ratelimit-resource": "core", **headers}
        stdout = "HTTP/2.0 %d X\n%s\n\n%s" % (status, "\n".join("%s: %s" % item for item in head.items()), json.dumps(body))
        return subprocess.CompletedProcess([], 0 if ok else 1, stdout, "" if ok else (stderr or "gh: HTTP %d" % status))

    def _refusal(self, kind: str, graphql: bool) -> subprocess.CompletedProcess:
        status, stderr, headers = REFUSALS[kind]
        if graphql and kind == "spent":
            # GraphQL is metered in points: GitHub refuses a query costing more than what is left
            # while the counter still reads above zero (seen 2026-10-07: refused at 41 of 5000).
            headers = {"x-ratelimit-remaining": "41", "x-ratelimit-reset": str(RESET), "x-ratelimit-resource": "graphql"}
            stderr = "gh: API rate limit exceeded for user ID 1. (HTTP 403)"
        return self._respond(status, {"message": stderr}, headers, stderr)

    def _graphql_call(self, args: list[str]) -> subprocess.CompletedProcess:
        query = next(arg[len("query="):] for arg in args if arg.startswith("query="))
        variables = dict(args[index + 1].partition("=")[::2] for index, arg in enumerate(args[:-1])
                         if arg in ("-f", "-F", "--raw-field", "--field") and not args[index + 1].startswith("query="))
        if "graphql" in self.refuse:
            self.log.append(Read("graphql", "graphql", REFUSALS[self.refuse["graphql"]][0]))
            return self._refusal(self.refuse["graphql"], True)
        cost = graphql_cost(query, variables)
        self.log.append(Read("graphql", "graphql", 200, cost))
        return self._respond(200, graphql_answer([self, *self.siblings], query, variables, cost),
                             {"x-ratelimit-resource": "graphql"})

    def _rest_call(self, args: list[str]) -> subprocess.CompletedProcess:
        endpoint = next(arg for arg in args[2:] if not arg.startswith("-") and ":" not in arg)
        kind = family(endpoint)
        if kind in self.refuse:
            self.log.append(Read(kind, endpoint, REFUSALS[self.refuse[kind]][0]))
            return self._refusal(self.refuse[kind], False)
        status, body = self._answer(endpoint)
        # GitHub sends no ETag for its rate limit, which is never the same twice.
        etag = None
        if status == 200 and kind != "rate_limit":
            etag = '"%s"' % hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        asked = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg in ("--header", "-H")]
        if etag and "If-None-Match: %s" % etag in asked:
            self.log.append(Read(kind, endpoint, 304))
            return self._respond(304, "", {"etag": etag})
        self.log.append(Read(kind, endpoint, status))
        return self._respond(status, body, {"etag": etag} if etag else {})

    def __call__(self, command, capture_output=True, text=True, timeout=None, **_):
        args = list(command)
        self._include = "--include" in args or "-i" in args
        return self._graphql_call(args) if "graphql" in args else self._rest_call(args)
