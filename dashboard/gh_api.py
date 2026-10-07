"""Bounded, read-only GitHub REST access through the signed-in `gh` CLI."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SHA = re.compile(r"[0-9a-f]{40}")


class ApiError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _split_response(stdout: str) -> tuple[int, dict[str, str], str]:
    """Split `gh api --include` output into the status code, lower-cased headers, and the body."""
    head, _, body = stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise ApiError("invalid_response") from None
    headers = {name.strip().lower(): value.strip()
               for name, _, value in (line.partition(":") for line in lines[1:])}
    return status, headers, body


def _graphql_command(query: str, variables: dict[str, str | int]) -> list[str]:
    command = ["gh", "api", "--include", "graphql", "-f", "query=" + query]
    for name, value in variables.items():
        if not name.isidentifier() or isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ApiError("invalid_response")
        # -f sends a string as written; -F is gh's typed form, used only for an integer.
        command.extend(("-f" if isinstance(value, str) else "-F", "%s=%s" % (name, value)))
    return command


def _graphql_body(stdout: str) -> dict:
    try:
        value = json.loads(_split_response(stdout)[2])
    except json.JSONDecodeError:
        raise ApiError("invalid_response") from None
    if not isinstance(value, dict):
        raise ApiError("invalid_response")
    return value


def _limit_family(endpoint: str) -> str:
    """The endpoint's path with its owner, repository, numeric IDs, and SHAs replaced by `*`.

    GitHub meters some endpoint families against a separate hourly counter, so a family whose
    counter is spent must not stop requests to the others."""
    parts = urlsplit(endpoint).path.strip("/").split("/")
    named = {"repos": 3, "orgs": 2, "users": 2}.get(parts[0], 1)
    return "/".join("*" if 0 < index < named or part.isdigit() or SHA.fullmatch(part) else part
                    for index, part in enumerate(parts))

class GitHubAPI:
    """A thread-safe reader with a hard budget counted per HTTP page.

    Responses carrying an ETag are kept and re-requested conditionally. GitHub answers an unchanged
    resource with 304, which does not count against its rate limit, so a 304 also returns its budget
    unit. Only entries used during the previous refresh survive into the next one."""

    def __init__(self, *, max_calls: int = 200, timeout: int = 30, clock=time.time,
                 runner=subprocess.run):
        self.max_calls = max_calls
        self.timeout = timeout
        self.clock = clock
        self.runner = runner
        self.calls = 0
        self.lowest_remaining: int | None = None
        self.backoff_until = 0.0
        self._family_backoff: dict[str, float] = {}
        self._lock = threading.Lock()
        self._generation = 0
        self._cache: dict[str, tuple[str, object, int]] = {}

    def begin(self) -> None:
        with self._lock:
            self.calls = 0
            self.lowest_remaining = None
            self._family_backoff = {family: until for family, until in self._family_backoff.items()
                                    if until > self.clock()}
            self._generation += 1
            self._cache = {endpoint: entry for endpoint, entry in self._cache.items()
                           if entry[2] >= self._generation - 1}
            if self.clock() < self.backoff_until:
                raise ApiError("rate_limited")

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _classify_error(stderr: str) -> str:
        lowered = stderr.lower()
        if "rate limit" in lowered or "http 429" in lowered:
            return "rate_limited"
        if "http 404" in lowered:
            return "not_found"
        if "http 401" in lowered or "authentication" in lowered:
            return "authentication_failed"
        if "http 403" in lowered:
            return "forbidden"
        return "unavailable"

    def _request(self, endpoint: str) -> object:
        family = _limit_family(endpoint)
        self._reserve(family)
        with self._lock:
            cached = self._cache.get(endpoint)
        command = ["gh", "api", "--include", endpoint]
        if cached:
            command += ["--header", "If-None-Match: %s" % cached[0]]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ApiError("unavailable") from None
        status, headers = _split_response(result.stdout)[:2] if result.stdout else (None, {})
        remaining = self._remember_remaining(headers)
        if result.returncode != 0:
            if cached and status == 304:
                with self._lock:
                    self.calls -= 1
                    self._cache[endpoint] = (cached[0], cached[1], self._generation)
                return copy.deepcopy(cached[1])
            code = self._classify_error(result.stderr)
            if code == "rate_limited":
                self._pause(family, headers, remaining == "0")
            raise ApiError(code)
        _, headers, body = _split_response(result.stdout)
        etag = headers.get("etag")
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            raise ApiError("invalid_response") from None
        if etag:
            with self._lock:
                self._cache[endpoint] = (etag, copy.deepcopy(value), self._generation)
        return value

    @staticmethod
    def _page_endpoint(endpoint: str, page: int) -> str:
        parts = urlsplit(endpoint)
        query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
                 if name not in {"page", "per_page"}]
        query.extend((("per_page", "100"), ("page", str(page))))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def page_items(self, endpoint: str, key: str | None = None) -> Iterator[tuple[list[dict], bool]]:
        """Yield validated 100-item pages and whether another page may exist."""
        page = 1
        while True:
            value = self._request(self._page_endpoint(endpoint, page))
            rows = value.get(key) if key and isinstance(value, dict) else value
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ApiError("invalid_response")
            may_have_more = len(rows) == 100
            yield rows, may_have_more
            if not may_have_more:
                return
            page += 1

    def pages(self, endpoint: str, key: str | None = None) -> list[list[dict]]:
        """Return explicit pages; each page consumes and reports one budget unit."""
        return [rows for rows, _ in self.page_items(endpoint, key)]

    def items(self, endpoint: str, key: str | None = None) -> list[dict]:
        return [row for page, _ in self.page_items(endpoint, key) for row in page]

    def one(self, endpoint: str) -> dict:
        value = self._request(endpoint)
        if not isinstance(value, dict):
            raise ApiError("invalid_response")
        return value

    def _reserve(self, family: str) -> None:
        with self._lock:
            if self.clock() < max(self.backoff_until, self._family_backoff.get(family, 0.0)):
                raise ApiError("rate_limited")
            if self.calls >= self.max_calls:
                raise ApiError("request_budget_exhausted")
            self.calls += 1

    def _remember_remaining(self, headers: dict[str, str]) -> str:
        remaining = headers.get("x-ratelimit-remaining", "")
        if remaining.isdigit():
            with self._lock:
                lowest = self.lowest_remaining
                self.lowest_remaining = int(remaining) if lowest is None else min(lowest, int(remaining))
        return remaining

    def _pause(self, family: str, headers: dict[str, str], spent: bool) -> None:
        """A spent hourly counter pauses only that family. Anything else pauses every request briefly."""
        try:
            reset = float(headers["x-ratelimit-reset"]) if spent else None
        except (KeyError, ValueError):
            reset = None
        with self._lock:
            if reset is None:
                self.backoff_until = max(self.backoff_until, self.clock() + 60)
            else:
                self._family_backoff[family] = max(self._family_backoff.get(family, 0.0), reset)

    def _keep_graphql_reserve(self, headers: dict[str, str]) -> None:
        """Stop asking GraphQL until its counter resets once less than half of it is left.

        The token's points are shared with everything else that uses the same App (seen 2026-10-07:
        a watched dashboard left 24 of 5000), and those users need them more than this page does."""
        try:
            remaining, limit, reset = (float(headers["x-ratelimit-" + name])
                                       for name in ("remaining", "limit", "reset"))
        except (KeyError, ValueError):
            return
        if remaining * 2 < limit:
            with self._lock:
                self._family_backoff["graphql"] = max(self._family_backoff.get("graphql", 0.0), reset)

    def graphql(self, query: str, variables: dict[str, str | int]) -> dict:
        """One GraphQL request. Counts as one budget unit and is not ETag-cached.

        GraphQL is a POST, so a later refresh cannot reuse it with If-None-Match the way REST reads can.
        """
        self._reserve("graphql")
        try:
            result = self.runner(_graphql_command(query, variables), capture_output=True, text=True,
                                 timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ApiError("unavailable") from None
        headers = _split_response(result.stdout)[1] if result.stdout else {}
        remaining = self._remember_remaining(headers)
        if result.returncode != 0:
            code = self._classify_error(result.stderr)
            if code == "rate_limited":
                # GraphQL is metered in points: GitHub refuses a query that costs more than what is left
                # while the counter still reads above zero (seen 2026-10-07: refused at 41 of 5000).
                # Only a secondary limit, which says so, is a reason to pause the REST reads too.
                secondary = "retry-after" in headers or "secondary" in result.stderr.lower()
                self._pause("graphql", headers, remaining == "0" or not secondary)
            raise ApiError(code)
        self._keep_graphql_reserve(headers)
        return _graphql_body(result.stdout)

    def rate(self) -> dict:
        value = self.one("rate_limit")
        core = (value.get("resources") or {}).get("core") or {}
        remaining, limit, reset = core.get("remaining"), core.get("limit"), core.get("reset")
        if not all(isinstance(item, int) for item in (remaining, limit, reset)):
            raise ApiError("invalid_response")
        if remaining <= 0:
            with self._lock:
                self.backoff_until = max(self.backoff_until, float(reset))
            raise ApiError("rate_limited")
        return {"remaining": remaining, "limit": limit, "resets_at_epoch": reset}
