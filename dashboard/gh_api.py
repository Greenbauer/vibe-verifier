"""Bounded, read-only GitHub REST access through the signed-in `gh` CLI."""

from __future__ import annotations

import json
import subprocess
import time


class ApiError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GitHubAPI:
    """A per-process reader with a hard request budget and rate-limit backoff."""

    def __init__(self, *, max_calls: int = 200, timeout: int = 30, clock=time.time, runner=subprocess.run):
        self.max_calls = max_calls
        self.timeout = timeout
        self.clock = clock
        self.runner = runner
        self.calls = 0
        self.backoff_until = 0.0

    def begin(self) -> None:
        self.calls = 0
        if self.clock() < self.backoff_until:
            raise ApiError("rate_limited")

    def _classify_error(self, stderr: str) -> str:
        lowered = stderr.lower()
        if "rate limit" in lowered or "http 429" in lowered:
            self.backoff_until = max(self.backoff_until, self.clock() + 60)
            return "rate_limited"
        if "http 404" in lowered:
            return "not_found"
        if "http 401" in lowered or "authentication" in lowered:
            return "authentication_failed"
        if "http 403" in lowered:
            return "forbidden"
        return "unavailable"

    def pages(self, endpoint: str) -> list:
        if self.calls >= self.max_calls:
            raise ApiError("request_budget_exhausted")
        self.calls += 1
        try:
            result = self.runner(["gh", "api", "--paginate", "--slurp", endpoint],
                                 capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ApiError("unavailable") from None
        if result.returncode != 0:
            raise ApiError(self._classify_error(result.stderr))
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ApiError("invalid_response") from None
        if not isinstance(value, list):
            raise ApiError("invalid_response")
        return value

    def items(self, endpoint: str, key: str | None = None) -> list[dict]:
        rows = []
        for page in self.pages(endpoint):
            values = page.get(key) if key and isinstance(page, dict) else page
            if not isinstance(values, list):
                raise ApiError("invalid_response")
            if not all(isinstance(row, dict) for row in values):
                raise ApiError("invalid_response")
            rows.extend(values)
        return rows

    def one(self, endpoint: str) -> dict:
        pages = self.pages(endpoint)
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise ApiError("invalid_response")
        return pages[0]

    def rate(self) -> dict:
        value = self.one("rate_limit")
        core = (value.get("resources") or {}).get("core") or {}
        remaining, limit, reset = core.get("remaining"), core.get("limit"), core.get("reset")
        if not all(isinstance(item, int) for item in (remaining, limit, reset)):
            raise ApiError("invalid_response")
        if remaining <= 0:
            self.backoff_until = max(self.backoff_until, float(reset))
            raise ApiError("rate_limited")
        return {"remaining": remaining, "limit": limit, "resets_at_epoch": reset}
