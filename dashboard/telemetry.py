"""Validation for the optional, atomically replaced telemetry snapshot."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import BOT_KEYS, Config
from .util import github_url, iso_time, parse_time

MAX_BYTES = 2 * 1024 * 1024
MAX_LANES = 1024
MAX_ACCOUNTS = 64
MAX_SAMPLES = 20_000
HISTORY = timedelta(days=7)
FUTURE_SKEW = timedelta(minutes=5)
STALE_AFTER = timedelta(minutes=5)
ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
LANE_STATES = {"busy", "allocated", "ready", "provisionable", "offline", "unknown"}
BOT_STATES = {"working", "idle", "down", "unknown"}


class TelemetryError(ValueError):
    """The snapshot cannot safely be shown."""


def _number(value: object, low: float = 0, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryError("metric is not numeric")
    number = float(value)
    if not math.isfinite(number) or number < low or (high is not None and number > high):
        raise TelemetryError("metric is outside its valid range")
    return number


def _text(value: object, length: int = 120) -> str:
    if not isinstance(value, str) or not value or len(value) > length or any(ord(char) < 32 for char in value):
        raise TelemetryError("text field is invalid")
    return value


def _timestamp(value: object, now: datetime) -> str:
    parsed = parse_time(value)
    if not parsed or parsed > now + FUTURE_SKEW:
        raise TelemetryError("timestamp is invalid")
    return iso_time(parsed)


def _same_owner(value: object, config: Config) -> None:
    if not isinstance(value, str) or value.lower() != config.owner.lower():
        raise TelemetryError("telemetry contains a different owner")


def _repository(value: object, config: Config) -> str:
    if not isinstance(value, str) or value.count("/") != 1:
        raise TelemetryError("job repository is invalid")
    owner, name = value.split("/", 1)
    if owner.lower() != config.owner.lower() or not name:
        raise TelemetryError("telemetry contains a cross-owner repository")
    return config.owner + "/" + name


def _section_time(section: dict, now: datetime) -> tuple[str, bool]:
    sampled_at = _timestamp(section.get("sampled_at"), now)
    stale = now - parse_time(sampled_at) > STALE_AFTER
    return sampled_at, stale


def _host(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TelemetryError("capacity.host must be an object")
    allowed = {"cpu_percent", "memory_used_bytes", "memory_total_bytes",
               "workspace_disk_free_bytes", "workspace_disk_total_bytes"}
    if set(value) - allowed:
        raise TelemetryError("capacity.host has unknown fields")
    result = {}
    for name, item in value.items():
        result[name] = _number(item, 0, 100 if name == "cpu_percent" else None)
    for used, total in (("memory_used_bytes", "memory_total_bytes"),
                        ("workspace_disk_free_bytes", "workspace_disk_total_bytes")):
        if used in result and total in result and result[used] > result[total]:
            raise TelemetryError("host metric exceeds its total")
    return result


def _lanes(value: object, config: Config) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_LANES:
        raise TelemetryError("capacity.lanes is invalid")
    lanes, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) - {"id", "state", "registered", "labels", "job", "runner_id"}:
            raise TelemetryError("lane row is invalid")
        lane_id, state = row.get("id"), row.get("state")
        if not isinstance(lane_id, str) or not ID.fullmatch(lane_id) or lane_id in seen or state not in LANE_STATES:
            raise TelemetryError("lane identity or state is invalid")
        seen.add(lane_id)
        registered = row.get("registered")
        if registered not in (True, False, None):
            raise TelemetryError("lane registered must be true, false, or null")
        labels = row.get("labels", [])
        if not isinstance(labels, list) or len(labels) > 20:
            raise TelemetryError("lane labels are invalid")
        lane = {"id": lane_id, "state": state, "registered": registered,
                "labels": [_text(label, 60) for label in labels]}
        if row.get("runner_id") is not None:
            runner_id = row["runner_id"]
            if isinstance(runner_id, bool) or not isinstance(runner_id, int) or runner_id <= 0:
                raise TelemetryError("runner id must be a positive integer")
            lane["runner_id"] = runner_id
        job = row.get("job")
        if job is not None:
            if not isinstance(job, dict) or set(job) - {"repository", "name", "url"}:
                raise TelemetryError("lane job is invalid")
            lane["job"] = {"repository": _repository(job.get("repository"), config),
                           "name": _text(job.get("name")),
                           "url": github_url(job.get("url"), config.owner)}
        elif state == "busy" and not (registered is True and lane.get("runner_id")):
            raise TelemetryError("a busy lane must identify its same-owner job")
        lanes.append(lane)
    return lanes


def _capacity(value: object, config: Config, now: datetime) -> dict:
    if value is None:
        return {"available": False, "reason": "not_provided"}
    if not isinstance(value, dict) or set(value) - {"sampled_at", "host", "lanes"}:
        raise TelemetryError("capacity section is invalid")
    sampled_at, stale = _section_time(value, now)
    lanes = _lanes(value.get("lanes", []), config)
    return {"available": True, "sampled_at": sampled_at, "stale": stale,
            "host": _host(value.get("host")), "lanes": lanes}


def _window(row: object, now: datetime) -> dict:
    if not isinstance(row, dict) or set(row) - {"name", "used_percent", "resets_at", "allowance_tokens", "window_minutes"}:
        raise TelemetryError("quota window is invalid")
    used = _number(row.get("used_percent"), 0, 100)
    reset = _timestamp(row.get("resets_at"), now + timedelta(days=3650))
    allowance, minutes = row.get("allowance_tokens"), row.get("window_minutes")
    if allowance is not None:
        allowance = int(_number(allowance, 1))
    # The window's length dates its start (reset minus length), which is what sizes it from tokens.
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= 60 * 24 * 31):
        raise TelemetryError("quota window length is invalid")
    return {"name": _text(row.get("name"), 40), "used_percent": used, "resets_at": reset,
            "allowance_tokens": allowance, "window_minutes": minutes}


def _usage(value: object, config: Config, now: datetime) -> dict:
    if value is None:
        return {"available": False, "reason": "not_provided"}
    if not isinstance(value, dict) or set(value) - {"sampled_at", "accounts", "samples", "completeness"}:
        raise TelemetryError("usage section is invalid")
    sampled_at, stale = _section_time(value, now)
    accounts = value.get("accounts")
    if not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS:
        raise TelemetryError("usage.accounts is invalid")
    clean_accounts, account_ids = [], set()
    for account in accounts:
        if not isinstance(account, dict) or set(account) - {"id", "label", "provider", "quota_windows"}:
            raise TelemetryError("usage account is invalid")
        account_id = account.get("id")
        if not isinstance(account_id, str) or not ID.fullmatch(account_id) or account_id in account_ids:
            raise TelemetryError("usage account id is invalid")
        account_ids.add(account_id)
        windows = account.get("quota_windows", [])
        if not isinstance(windows, list) or len(windows) > 10:
            raise TelemetryError("quota_windows is invalid")
        clean_accounts.append({"id": account_id, "label": _text(account.get("label")),
                               "provider": _text(account.get("provider"), 60),
                               "quota_windows": [_window(item, now) for item in windows]})
    samples = value.get("samples", [])
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise TelemetryError("usage.samples is invalid")
    clean_samples = []
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {"owner", "account", "bot", "timestamp", "input_tokens", "output_tokens"}:
            raise TelemetryError("token sample is invalid")
        _same_owner(sample["owner"], config)
        if sample["account"] not in account_ids or sample["bot"] not in (*BOT_KEYS, *(agent.id for agent in config.agents)):
            raise TelemetryError("token sample references an unknown account or bot")
        timestamp = _timestamp(sample["timestamp"], now)
        if now - parse_time(timestamp) > HISTORY:
            continue
        clean_samples.append({"account": sample["account"], "bot": sample["bot"], "timestamp": timestamp,
                              "input_tokens": int(_number(sample["input_tokens"])),
                              "output_tokens": int(_number(sample["output_tokens"]))})
    completeness = value.get("completeness")
    if not isinstance(completeness, str) or len(completeness) > 240:
        raise TelemetryError("usage.completeness must describe source coverage")
    return {"available": True, "sampled_at": sampled_at, "stale": stale, "accounts": clean_accounts,
            "samples": clean_samples, "completeness": completeness}


def _bots(value: object, config: Config, now: datetime) -> dict:
    if value is None:
        return {"available": False, "reason": "not_provided"}
    if not isinstance(value, dict) or set(value) - {"sampled_at", "states"}:
        raise TelemetryError("bots section is invalid")
    sampled_at, stale = _section_time(value, now)
    states, seen = [], set()
    for row in value.get("states", []):
        if not isinstance(row, dict) or set(row) - {"owner", "bot", "state", "detail"}:
            raise TelemetryError("bot state is invalid")
        _same_owner(row.get("owner"), config)
        bot, state = row.get("bot"), row.get("state")
        if bot not in BOT_KEYS or bot in seen or state not in BOT_STATES:
            raise TelemetryError("bot identity or state is invalid")
        seen.add(bot)
        states.append({"bot": bot, "state": state,
                       "detail": _text(row["detail"], 160) if row.get("detail") else None})
    return {"available": True, "sampled_at": sampled_at, "stale": stale, "states": states}


def _agents(value: object, config: Config, now: datetime) -> dict:
    if value is None:
        return {"available": False}
    if not isinstance(value, dict) or set(value) != {"sampled_at", "rows"}:
        raise TelemetryError("agent snapshot is invalid")
    sampled, stale = _section_time(value, now)
    if not isinstance(value["rows"], list) or len(value["rows"]) > 64:
        raise TelemetryError("agent roster is invalid")
    allowed = {agent.id: agent for agent in config.agents if not agent.workflow_role}
    clean, seen = [], set()
    for row in value["rows"]:
        if (not isinstance(row, dict) or set(row) != {"id", "state", "runs"}
                or row.get("id") not in allowed or row["id"] in seen
                or row.get("state") not in {"working", "idle", "down", "paused", "unknown"}
                or not isinstance(row.get("runs"), list) or len(row["runs"]) > 5):
            raise TelemetryError("agent identity or state is invalid")
        seen.add(row["id"])
        runs = []
        for run in row["runs"]:
            if (not isinstance(run, dict) or set(run) != {"id", "completed_at", "category", "elapsed_seconds"}
                    or not isinstance(run["id"], str) or not ID.fullmatch(run["id"])
                    or run["category"] not in {"success", "failed", "cancelled"}):
                raise TelemetryError("agent run is invalid")
            completed = _timestamp(run["completed_at"], now)
            if now - parse_time(completed) > HISTORY:
                continue
            runs.append({"id": run["id"], "completed_at": completed, "category": run["category"],
                         "elapsed_seconds": _number(run["elapsed_seconds"]),
                         "name": allowed[row["id"]].name + " run", "repository": None, "html_url": None})
        clean.append({"id": row["id"], "state": row["state"], "runs": runs})
    return {"available": True, "sampled_at": sampled, "stale": stale, "rows": clean}


def _read(path: Path) -> object:
    try:
        info = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise TelemetryError("telemetry file is unsafe or too large")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise TelemetryError("telemetry file owner does not match")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise TelemetryError("telemetry file permissions are unsafe")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TelemetryError("telemetry file is unavailable") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TelemetryError("telemetry file is unreadable") from error


def read_telemetry(config: Config, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if config.telemetry_file is None:
        return {"available": False, "reason": "not_configured"}
    try:
        value = _read(config.telemetry_file)
        if isinstance(value, dict) and "hosts" in value:
            from .collector_view import collector_view
            try:
                return collector_view(value, config, now)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise TelemetryError("invalid collector snapshot") from None
        if not isinstance(value, dict) or set(value) - {"version", "owner", "capacity", "usage", "bots", "agents"}:
            raise TelemetryError("telemetry root is invalid")
        if value.get("version") != 1:
            raise TelemetryError("telemetry version must be 1")
        _same_owner(value.get("owner"), config)
        return {"available": True, "capacity": _capacity(value.get("capacity"), config, now),
                "usage": _usage(value.get("usage"), config, now),
                "bots": _bots(value.get("bots"), config, now),
                "agents": _agents(value.get("agents"), config, now)}
    except TelemetryError:
        return {"available": False, "reason": "invalid_or_unavailable"}
