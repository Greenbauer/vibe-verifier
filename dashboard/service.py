"""Memory-only polling cache with honest source age and coalesced refreshes."""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import BOT_KEYS, Config
from .gh_api import ApiError
from .github import GitHubCollector, recent_bot_runs
from .telemetry import read_telemetry
from .util import parse_time

ACTIVE_TTL_SECONDS = 60
INVENTORY_TTL_SECONDS = 300
STALE_GRACE_SECONDS = 180
TRANSIENT = {"rate_limited", "unavailable", "request_budget_exhausted", "invalid_response"}
PRIVATE_FAILURES = {"authentication_failed", "forbidden", "not_found"}


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
        self._condition = threading.Condition()
        self._refreshing = False

    def _empty_github(self, code: str) -> dict:
        repositories = [self._unavailable_repository(repository, code)
                        for repository in self.config.repositories]
        return {"owner": self.config.owner, "sampled_at": None, "repositories": repositories,
                "coverage": {"selected": len(self.config.repositories), "readable": 0,
                             "label": "Selected repositories", "inventory": {}},
                "bots": {"partial": True, "roles": {}, "errors": [{"code": code}]},
                "errors": [{"code": code}], "partial": True,
                "api": {"calls": 0, "max_calls": self.collector.api.max_calls}}

    @staticmethod
    def _within_grace(sampled_at: object, now: datetime) -> bool:
        sampled = parse_time(sampled_at)
        return sampled is not None and now - sampled <= timedelta(seconds=STALE_GRACE_SECONDS)

    def _stale_allowed(self, row: dict, code: str, _now: datetime) -> bool:
        # A transient miss keeps the last observation. Age marks it stale; it does not delete it.
        return code in TRANSIENT and bool(row.get("sampled_at"))

    def _merge(self, fresh: dict, previous: dict | None, now: datetime) -> dict:
        current = {row["repository"]: row for row in fresh["repositories"]}
        old = {row["repository"]: row for row in (previous or {}).get("repositories", [])}
        errors = {row.get("repository"): row.get("code", "unavailable") for row in fresh.get("errors", [])}
        sampled_at = fresh.get("sampled_at")
        for row in current.values():
            row.setdefault("sampled_at", sampled_at)
            row["stale"] = False
        for repository in self.config.repositories:
            if repository in current:
                continue
            code = errors.get(repository, "unavailable")
            if repository in old and self._stale_allowed(old[repository], code, now):
                row = copy.deepcopy(old[repository])
                row["stale"], row["source_error"] = True, code
                current[repository] = row
            else:
                current[repository] = self._unavailable_repository(repository, code, fresh)
        fresh["repositories"] = [current[name] for name in self.config.repositories]
        fresh["coverage"]["readable"] = sum(not row.get("unavailable") for row in fresh["repositories"])
        return fresh

    @staticmethod
    def _unavailable_repository(repository: str, code: str, source: dict | None = None) -> dict:
        inventory = (source or {}).get("coverage", {}).get("inventory", {})
        return {"repository": repository,
                "subscription": inventory.get(repository, {}).get("subscription", "unknown"),
                "pulls": [], "errors": [{"code": code}], "sampled_at": None,
                "stale": False, "unavailable": True}

    def _mark_transient(self, previous: dict, code: str, _now: datetime) -> dict:
        github = copy.deepcopy(previous)
        github["partial"], github["stale"], github["errors"] = True, True, [{"code": code}]
        for row in github.get("repositories", []):
            if row.get("sampled_at"):
                row["stale"], row["source_error"] = True, code
        for role in github.get("bots", {}).get("roles", {}).values():
            role["stale"], role["source_error"] = True, code
        github.setdefault("bots", {})["partial"] = True
        return github

    @staticmethod
    def _source_error_codes(fresh: dict) -> set[str]:
        rows = list(fresh.get("errors", [])) + list(fresh.get("bots", {}).get("errors", []))
        for repository in fresh.get("repositories", []):
            rows.extend(repository.get("errors", []))
        return {row.get("code") for row in rows if isinstance(row, dict) and isinstance(row.get("code"), str)}

    def _clear_collector_cache(self) -> None:
        clear = getattr(self.collector, "clear_private_cache", None)
        if clear:
            clear()

    def _perform_refresh(self, requested_mono: float, requested_wall: datetime) -> None:
        with self._condition:
            previous = copy.deepcopy(self._github)
            inventory_fresh = (bool(self._inventory)
                               and requested_mono - self._inventory_at < INVENTORY_TTL_SECONDS)
            inventory = copy.deepcopy(self._inventory) if inventory_fresh else None
        new_inventory = None
        inventory_refreshed = inventory is None
        try:
            fresh = self.collector.collect(inventory)
            codes = self._source_error_codes(fresh)
            if codes & PRIVATE_FAILURES:
                self._clear_collector_cache()
                with self._condition:
                    self._inventory, self._inventory_at = {}, 0.0
            result = self._merge(fresh, previous, requested_wall)
            if inventory_refreshed:
                new_inventory = fresh.get("coverage", {}).get("inventory") or None
        except ApiError as error:
            if error.code in PRIVATE_FAILURES or previous is None:
                result = self._empty_github(error.code)
                if error.code in PRIVATE_FAILURES:
                    self._clear_collector_cache()
                    with self._condition:
                        self._inventory, self._inventory_at = {}, 0.0
            elif error.code in TRANSIENT:
                result = self._mark_transient(previous, error.code, requested_wall)
            else:
                result = self._empty_github(error.code)
        except Exception:
            result = self._mark_transient(previous, "unavailable", requested_wall) if previous else self._empty_github("unavailable")
        completed_mono = self.monotonic()
        with self._condition:
            self._github = result
            self._github_at = completed_mono
            if new_inventory:
                self._inventory = {**self._inventory, **new_inventory}
                self._inventory_at = completed_mono
            self._refreshing = False
            self._condition.notify_all()

    def _background_refresh(self, now_mono: float, now: datetime) -> None:
        self._perform_refresh(now_mono, now)

    def _expire(self, github: dict, now: datetime) -> dict:
        """Mark observations past the grace window stale and keep their last rows."""
        for row in github.get("repositories", []):
            if row.get("unavailable"):
                continue
            if row.get("sampled_at") and not self._within_grace(row.get("sampled_at"), now):
                row["stale"] = True
                github["partial"] = True
        github.get("coverage", {})["readable"] = sum(
            not row.get("unavailable") for row in github.get("repositories", []))
        for role in github.get("bots", {}).get("roles", {}).values():
            aged = any(stamp and not self._within_grace(stamp, now)
                       for stamp in (role.get("sampled_at"), role.get("history_sampled_at")))
            if aged:
                role["stale"] = True
                github.setdefault("bots", {})["partial"] = True
                github["partial"] = True
            role["recent_2h"] = recent_bot_runs(role.get("recent_2h", []), now, 2)
            role["recent_7d"] = recent_bot_runs(role.get("recent_7d", []), now, 168)
            history = role["recent_7d"]
            role["latest_failure"] = history[0] if history and history[0].get("category") == "failed" else None
            if role.get("active"):
                role["state"] = "working"
        github["stale"] = any(row.get("stale") for row in github.get("repositories", [])) or any(
            role.get("stale") for role in github.get("bots", {}).get("roles", {}).values())
        return github

    def _merge_bot_states(self, github: dict, telemetry: dict) -> None:
        roles = github.setdefault("bots", {}).setdefault("roles", {})
        telemetry_bots = telemetry.get("bots", {}) if telemetry.get("available") else {}
        states = {row["bot"]: row for row in telemetry_bots.get("states", [])}
        telemetry_current = telemetry_bots.get("available") and not telemetry_bots.get("stale")
        for role in BOT_KEYS:
            result = roles.setdefault(role, {"active": [], "recent_2h": [], "recent_7d": [],
                                             "latest_failure": None, "state": "unknown"})
            if result.get("active"):
                result["state"], result["state_source"] = "working", "github_actions"
            elif telemetry_current and role in states:
                result["state"], result["state_source"] = states[role]["state"], "telemetry"
                result["state_detail"] = states[role].get("detail")
            elif result.get("state") == "idle":
                result["state_source"] = "github_actions"
            elif role in states:
                result["state"], result["state_source"] = states[role]["state"], "telemetry"
                result["state_detail"] = states[role].get("detail")
                result["stale"] = True
            elif result.get("state") in (None, "unknown"):
                result["state"], result["state_source"] = "unknown", "unavailable"
        if any(role.get("stale") for role in roles.values()):
            github["stale"] = True
            github["partial"] = True

    def snapshot(self, *, force: bool = False, nonblocking: bool = False) -> dict:
        now_mono, now = self.monotonic(), self.wall_clock().astimezone(timezone.utc)
        run_refresh = False
        thread = None
        with self._condition:
            waited = False
            while self._refreshing and not nonblocking:
                waited = True
                self._condition.wait()
            due = force or self._github is None or now_mono - self._github_at >= ACTIVE_TTL_SECONDS
            if due and not self._refreshing and not waited:
                self._refreshing = True
                if nonblocking:
                    thread = threading.Thread(target=self._background_refresh, args=(now_mono, now),
                                              name="dashboard-github-refresh", daemon=True)
                else:
                    run_refresh = True
            if not run_refresh:
                github = copy.deepcopy(self._github or self._empty_github("loading"))
                github["refreshing"] = self._refreshing
        if thread:
            thread.start()
        if run_refresh:
            self._perform_refresh(now_mono, now)
            with self._condition:
                github = copy.deepcopy(self._github)
                github["refreshing"] = False
        github = self._expire(github, self.wall_clock().astimezone(timezone.utc))
        telemetry = read_telemetry(self.config, self.wall_clock().astimezone(timezone.utc))
        self._merge_bot_states(github, telemetry)
        return {"version": 1, "owner": self.config.owner, "github": github, "telemetry": telemetry}
