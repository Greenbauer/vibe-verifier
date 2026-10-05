"""Bounded, read-only GitHub REST access through the signed-in `gh` CLI."""

from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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
        self.backoff_until = 0.0
        self._lock = threading.Lock()
        self._generation = 0
        self._cache: dict[str, tuple[str, object, int]] = {}

    def begin(self) -> None:
        with self._lock:
            self.calls = 0
            self._generation += 1
            self._cache = {endpoint: entry for endpoint, entry in self._cache.items()
                           if entry[2] >= self._generation - 1}
            if self.clock() < self.backoff_until:
                raise ApiError("rate_limited")

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _classify_error(self, stderr: str) -> str:
        lowered = stderr.lower()
        if "rate limit" in lowered or "http 429" in lowered:
            with self._lock:
                self.backoff_until = max(self.backoff_until, self.clock() + 60)
            return "rate_limited"
        if "http 404" in lowered:
            return "not_found"
        if "http 401" in lowered or "authentication" in lowered:
            return "authentication_failed"
        if "http 403" in lowered:
            return "forbidden"
        return "unavailable"

    def _request(self, endpoint: str) -> object:
        with self._lock:
            if self.clock() < self.backoff_until:
                raise ApiError("rate_limited")
            if self.calls >= self.max_calls:
                raise ApiError("request_budget_exhausted")
            self.calls += 1
            cached = self._cache.get(endpoint)
        command = ["gh", "api", "--include", endpoint]
        if cached:
            command += ["--header", "If-None-Match: %s" % cached[0]]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ApiError("unavailable") from None
        if result.returncode != 0:
            status, headers = _split_response(result.stdout)[:2] if result.stdout else (None, {})
            if cached and status == 304:
                with self._lock:
                    self.calls -= 1
                    self._cache[endpoint] = (cached[0], cached[1], self._generation)
                return copy.deepcopy(cached[1])
            code = self._classify_error(result.stderr)
            # Each owner's repositories can draw on a different hourly bucket, so the reset GitHub
            # reports on the refused request is the only reliable time to try again.
            if code == "rate_limited" and headers.get("x-ratelimit-remaining") == "0":
                try:
                    reset = float(headers.get("x-ratelimit-reset", ""))
                except ValueError:
                    reset = 0.0
                with self._lock:
                    self.backoff_until = max(self.backoff_until, reset)
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
