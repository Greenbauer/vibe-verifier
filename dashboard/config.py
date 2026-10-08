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
class AgentDefinition:
    id: str
    name: str
    role: str
    workflow_role: str | None = None


def _parse_agents(value: object) -> tuple[AgentDefinition, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise ConfigError("agents must be a roster of at most 64 identities")
    result, ids, names, workflows = [], set(), set(), set()
    for row in value:
        if not isinstance(row, dict):
            raise ConfigError("agent must be an object")
        _only_keys(row, {"id", "name", "role", "workflow_role"}, "agent")
        identity, name, role = row.get("id"), row.get("name"), row.get("role")
        workflow = row.get("workflow_role")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", identity)
                or role not in ("swe", "qae") or not isinstance(name, str)
                or not re.fullmatch(role.upper() + r"[0-9]*", name)
                or identity in (*BOT_KEYS, "total") or identity in ids or name in names):
            raise ConfigError("agent identity must uniquely name an SWE or QAE")
        if workflow is not None and (workflow != {"swe": "reviewer", "qae": "explorer"}[role]
                                      or workflow in workflows or name != role.upper()):
            raise ConfigError("aggregate CI activity cannot identify a numbered agent or a verification gate")
        ids.add(identity)
        names.add(name)
        if workflow:
            workflows.add(workflow)
        result.append(AgentDefinition(identity, name, role, workflow))
    return tuple(result)


@dataclass(frozen=True)
class Config:
    owner: str
    repositories: tuple[str, ...]
    bots: dict[str, BotDefinition]
    telemetry_file: Path | None
    proxy_origin: str | None = None
    agents: tuple[AgentDefinition, ...] = ()
    # True when repositories is the string "all": the set is discovered on each refresh.
    all_repositories: bool = False


def coverage_label(config: Config) -> str:
    """What the pull-request banner counts. A list is a selection; "all" is the whole owner."""
    if config.all_repositories:
        return "All repositories of %s" % config.owner
    return "Selected repositories"


def _owned_regular_file(path: Path, uid: int | None, *, may_be_missing: bool = False) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if may_be_missing:
            return
        raise ConfigError("configuration file does not exist") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError("path must be a regular file, not a symlink")
    if uid is not None and info.st_uid != uid:
        raise ConfigError("file must be owned by the dashboard user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError("file must not be group- or world-writable")


def _read_json(path: Path, uid: int | None) -> object:
    _owned_regular_file(path, uid)
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


def load_config(filename: str | os.PathLike[str], uid: int | None = None) -> Config:
    """Load once at process startup; callers retain the returned frozen value.

    The configuration and telemetry files must belong to uid, by default this process's user. A root
    helper passes the dashboard user's uid, so it accepts exactly what the dashboard itself will."""
    if uid is None and hasattr(os, "getuid"):
        uid = os.getuid()
    path = Path(filename).expanduser()
    value = _read_json(path, uid)
    if not isinstance(value, dict):
        raise ConfigError("configuration must be a JSON object")
    _only_keys(value, {"version", "owner", "repositories", "bots", "telemetry_file", "proxy_origin", "agents"},
               "configuration")
    if value.get("version") != 1:
        raise ConfigError("configuration version must be 1")
    owner = value.get("owner")
    if not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise ConfigError("owner is not a valid GitHub owner")
    # Either a non-empty list of this owner's repositories, or the exact string "all"
    # (every non-archived repository of the owner, discovered on each refresh). The two
    # shapes are different JSON types, so a list cannot mean "all" and the string cannot
    # name a repository.
    repositories = value.get("repositories", None)
    if repositories == "all":
        canonical, all_repositories = [], True
    elif isinstance(repositories, list) and repositories:
        all_repositories = False
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
    else:
        raise ConfigError('repositories must be a non-empty list of OWNER/NAME or the string "all"')
    telemetry = value.get("telemetry_file")
    telemetry_path = None
    if telemetry is not None:
        if not isinstance(telemetry, str) or not Path(telemetry).is_absolute():
            raise ConfigError("telemetry_file must be an absolute path")
        telemetry_path = Path(telemetry)
        _owned_regular_file(telemetry_path, uid, may_be_missing=True)
    return Config(owner, tuple(canonical), _parse_bots(value.get("bots")), telemetry_path,
                  _parse_proxy_origin(value.get("proxy_origin")), _parse_agents(value.get("agents", [])),
                  all_repositories)
