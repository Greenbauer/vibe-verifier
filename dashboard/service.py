"""Memory-only polling cache that keeps stale source timestamps honest."""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import BOT_KEYS, Config
from .gh_api import ApiError
from .github import GitHubCollector
from .telemetry import read_telemetry
from .util import parse_time

ACTIVE_TTL_SECONDS = 60
INVENTORY_TTL_SECONDS = 300
STALE_GRACE_SECONDS = 180
TRANSIENT = {"rate_limited", "unavailable", "request_budget_exhausted", "invalid_response"}


class DashboardService:
    def __init__(self, config: Config, collector: GitHubCollector | None = None,
                 *, monotonic=time.monotonic, wall_clock=None):
        self.config = config
        self.collector = collector or GitHubCollector(config)
        self.monotonic = monotonic
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._github: dict | None = None
        self._github_at = 0.0
        self._inventory: dict[str, dict] = {}
        self._inventory_at = 0.0
        self._lock = threading.Lock()

    def _empty_github(self, code: str) -> dict:
        return {"owner": self.config.owner, "sampled_at": None, "repositories": [],
                "coverage": {"selected": len(self.config.repositories), "readable": 0,
                             "label": "Selected repositories", "inventory": {}},
                "bots": {"partial": True, "roles": {}}, "errors": [{"code": code}],
                "partial": True, "api": {"calls": 0, "max_calls": self.collector.api.max_calls}}

    def _stale_allowed(self, row: dict, code: str, now: datetime) -> bool:
        sampled = parse_time(row.get("sampled_at"))
        return code in TRANSIENT and sampled is not None and now - sampled <= timedelta(seconds=STALE_GRACE_SECONDS)

    def _merge(self, fresh: dict, previous: dict | None, now: datetime) -> dict:
        current = {row["repository"]: row for row in fresh["repositories"]}
        old = {row["repository"]: row for row in (previous or {}).get("repositories", [])}
        errors = {row.get("repository"): row.get("code", "unavailable") for row in fresh.get("errors", [])}
        sampled_at = fresh.get("sampled_at")
        for repository, row in current.items():
            row["sampled_at"], row["stale"] = sampled_at, False
        for repository in self.config.repositories:
            if repository in current:
                continue
            code = errors.get(repository, "unavailable")
            if repository in old and self._stale_allowed(old[repository], code, now):
                row = copy.deepcopy(old[repository])
                row["stale"], row["source_error"] = True, code
                current[repository] = row
            else:
                current[repository] = {"repository": repository, "subscription":
                                       fresh.get("coverage", {}).get("inventory", {}).get(repository, {}).get("subscription", "unknown"),
                                       "pulls": [], "errors": [{"code": code}], "sampled_at": None,
                                       "stale": False, "unavailable": True}
        fresh["repositories"] = [current[name] for name in self.config.repositories]
        successes = [row for row in fresh["repositories"] if row.get("sampled_at") == sampled_at]
        if not successes:
            fresh["sampled_at"] = (previous or {}).get("sampled_at")
        fresh["coverage"]["readable"] = len(successes)
        return fresh

    def _refresh(self, now_mono: float, now: datetime) -> None:
        inventory = self._inventory if now_mono - self._inventory_at < INVENTORY_TTL_SECONDS else None
        try:
            fresh = self.collector.collect(inventory)
            fresh = self._merge(fresh, self._github, now)
            new_inventory = fresh.get("coverage", {}).get("inventory") or {}
            if new_inventory:
                self._inventory = {**self._inventory, **new_inventory}
                self._inventory_at = now_mono
            self._github = fresh
        except ApiError as error:
            if self._github is None:
                self._github = self._empty_github(error.code)
            else:
                self._github["partial"] = True
                self._github["errors"] = [{"code": error.code}]
                for row in self._github.get("repositories", []):
                    if self._stale_allowed(row, error.code, now):
                        row["stale"], row["source_error"] = True, error.code
                    else:
                        repository = row.get("repository")
                        row.clear()
                        row.update({"repository": repository, "subscription": "unknown",
                                    "pulls": [], "errors": [{"code": error.code}], "sampled_at": None,
                                    "stale": False, "unavailable": True})
        self._github_at = now_mono

    def _merge_bot_states(self, github: dict, telemetry: dict) -> None:
        roles = github.setdefault("bots", {}).setdefault("roles", {})
        telemetry_bots = telemetry.get("bots", {}) if telemetry.get("available") else {}
        states = {row["bot"]: row for row in telemetry_bots.get("states", [])}
        telemetry_current = telemetry_bots.get("available") and not telemetry_bots.get("stale")
        for role in BOT_KEYS:
            result = roles.setdefault(role, {"active": [], "recent_2h": [], "recent_7d": [], "latest_failure": None})
            if result.get("active"):
                result["state"], result["state_source"] = "working", "github_actions"
            elif telemetry_current and role in states:
                result["state"], result["state_source"] = states[role]["state"], "telemetry"
                result["state_detail"] = states[role].get("detail")
            else:
                result["state"], result["state_source"] = "unknown", "unavailable"

    def snapshot(self, *, force: bool = False) -> dict:
        with self._lock:
            now_mono, now = self.monotonic(), self.wall_clock().astimezone(timezone.utc)
            if force or self._github is None or now_mono - self._github_at >= ACTIVE_TTL_SECONDS:
                self._refresh(now_mono, now)
            github = copy.deepcopy(self._github)
            telemetry = read_telemetry(self.config, now)
            self._merge_bot_states(github, telemetry)
            return {"version": 1, "owner": self.config.owner, "github": github, "telemetry": telemetry}
