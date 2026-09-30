"""Strict, immutable-at-startup dashboard configuration."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
WORKFLOW = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
HOSTNAME = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z")
BOT_KEYS = ("reviewer", "explorer", "verifier")
MAX_CONFIG_BYTES = 128 * 1024


class ConfigError(ValueError):
    """The local configuration is unsafe or malformed."""


@dataclass(frozen=True)
class BotDefinition:
    workflow: str
    jobs: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    owner: str
    repositories: tuple[str, ...]
    bots: dict[str, BotDefinition]
    telemetry_file: Path | None
    proxy_origin: str | None = None


def _owned_regular_file(path: Path, *, may_be_missing: bool = False) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if may_be_missing:
            return
        raise ConfigError("configuration file does not exist") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError("path must be a regular file, not a symlink")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigError("file must be owned by the dashboard user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError("file must not be group- or world-writable")


def _read_json(path: Path) -> object:
    _owned_regular_file(path)
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ConfigError("configuration file is too large")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError("configuration file is not valid UTF-8 JSON") from error


def _only_keys(value: dict, allowed: set[str], where: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ConfigError("unknown %s field(s): %s" % (where, ", ".join(sorted(extra))))


def _parse_bots(value: object) -> dict[str, BotDefinition]:
    if not isinstance(value, dict) or set(value) != set(BOT_KEYS):
        raise ConfigError("bots must define reviewer, explorer, and verifier")
    result = {}
    for role in BOT_KEYS:
        row = value[role]
        if not isinstance(row, dict):
            raise ConfigError("bots.%s must be an object" % role)
        _only_keys(row, {"workflow", "jobs"}, "bots.%s" % role)
        workflow, jobs = row.get("workflow"), row.get("jobs")
        if not isinstance(workflow, str) or not WORKFLOW.fullmatch(workflow):
            raise ConfigError("bots.%s.workflow must be a workflow file name" % role)
        if not isinstance(jobs, list) or not jobs or not all(
                isinstance(job, str) and 0 < len(job) <= 120 for job in jobs):
            raise ConfigError("bots.%s.jobs must be a non-empty list of job names" % role)
        if len(set(jobs)) != len(jobs):
            raise ConfigError("bots.%s.jobs contains a duplicate" % role)
        result[role] = BotDefinition(workflow, tuple(jobs))
    return result


def _parse_proxy_origin(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "*" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError("proxy_origin must be a valid HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ConfigError("proxy_origin must be a valid HTTPS origin") from error
    hostname = parsed.hostname
    authority = hostname if port is None else "%s:%s" % (hostname, port)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or value != "https://" + authority or hostname is None
            or len(hostname) > 253 or not HOSTNAME.fullmatch(hostname)
            or port is not None and not 1 <= port <= 65535):
        raise ConfigError("proxy_origin must be a valid HTTPS origin")
    return value


def load_config(filename: str | os.PathLike[str]) -> Config:
    """Load once at process startup; callers retain the returned frozen value."""
    path = Path(filename).expanduser()
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ConfigError("configuration must be a JSON object")
    _only_keys(value, {"version", "owner", "repositories", "bots", "telemetry_file", "proxy_origin"},
               "configuration")
    if value.get("version") != 1:
        raise ConfigError("configuration version must be 1")
    owner = value.get("owner")
    if not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise ConfigError("owner is not a valid GitHub owner")
    repositories = value.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ConfigError("repositories must be a non-empty list")
    canonical = []
    for repository in repositories:
        if not isinstance(repository, str) or repository.count("/") != 1:
            raise ConfigError("each repository must be OWNER/NAME")
        repo_owner, name = repository.split("/", 1)
        if repo_owner.lower() != owner.lower() or not REPOSITORY.fullmatch(name):
            raise ConfigError("repository %r is outside configured owner %s" % (repository, owner))
        canonical.append(owner + "/" + name)
    if len(set(name.lower() for name in canonical)) != len(canonical):
        raise ConfigError("repositories contains a duplicate")
    telemetry = value.get("telemetry_file")
    telemetry_path = None
    if telemetry is not None:
        if not isinstance(telemetry, str) or not Path(telemetry).is_absolute():
            raise ConfigError("telemetry_file must be an absolute path")
        telemetry_path = Path(telemetry)
        _owned_regular_file(telemetry_path, may_be_missing=True)
    return Config(owner, tuple(canonical), _parse_bots(value.get("bots")), telemetry_path,
                  _parse_proxy_origin(value.get("proxy_origin")))
