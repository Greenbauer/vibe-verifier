#!/usr/bin/env python3
"""Collect one owner's CI lane telemetry into an atomic local JSON snapshot."""

import argparse
import base64
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import runpy
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
SCHEMA_VERSION = 1
QUOTA_INTERVAL_SECONDS = 300
HISTORY_SECONDS = 7 * 24 * 60 * 60
MAX_SAMPLES = 20160
MAX_CONFIG = 64 * 1024
MAX_SNAPSHOT = 8 * 1024 * 1024
MAX_REMOTE_OUTPUT = 1024 * 1024
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
DESTINATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:\[\]-]{0,254}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SAFE_ERRORS = {"collection_failed", "host_metrics_unavailable", "invalid_arguments",
               "listener_budget_invalid", "listener_config_unavailable", "listener_identity_mismatch",
               "foreign_job_target", "remote_failed", "remote_output_invalid", "ssh_failed",
               "ssh_timeout", "quota_unavailable"}

class CollectorError(Exception):
    """A safe local collection error code."""


@dataclass(frozen=True)
class CollectorConfig:
    owner: str
    host_label: str
    ssh_argv: tuple
    destination: str
    listener_config_path: str
    lane_name: str
    workspace_path: str
    codex_home: str

def _read_file(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("file is too large")
        return b"".join(chunks)
    finally:
        os.close(fd)

def _exact_keys(value, required, where):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError("%s has invalid fields" % where)
    return value

def _absolute(value, field):
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or len(value) > 4096:
        raise ValueError("%s must be an absolute path" % field)
    return value

def load_config(path):
    try:
        data = json.loads(_read_file(path, MAX_CONFIG).decode("utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("collector config is unreadable") from error
    _exact_keys(data, ("version", "owner", "host"), "config")
    if data["version"] != 1 or not isinstance(data["owner"], str) or not OWNER_RE.fullmatch(data["owner"]):
        raise ValueError("config version or owner is invalid")
    fields = ("label", "listener_config_path", "lane_name", "workspace_path", "codex_home")
    remote = isinstance(data["host"], dict) and ("ssh_argv" in data["host"] or "destination" in data["host"])
    host = _exact_keys(data["host"], fields + (("ssh_argv", "destination") if remote else ()), "host")
    if (not isinstance(host["label"], str) or not 1 <= len(host["label"]) <= 100
            or any(ord(char) < 32 for char in host["label"])):
        raise ValueError("host label is invalid")
    ssh, destination = host.get("ssh_argv", []), host.get("destination", "")
    if remote and (not isinstance(ssh, list) or not 1 <= len(ssh) <= 32
                   or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in ssh)
                   or os.path.basename(ssh[0]) != "ssh"):
        raise ValueError("ssh_argv must invoke ssh")
    if remote and (not isinstance(destination, str) or not DESTINATION_RE.fullmatch(destination)):
        raise ValueError("SSH destination is invalid")
    if not isinstance(host["lane_name"], str) or not LANE_RE.fullmatch(host["lane_name"]):
        raise ValueError("lane name is invalid")
    return CollectorConfig(
        owner=data["owner"], host_label=host["label"], ssh_argv=tuple(ssh),
        destination=destination, listener_config_path=_absolute(
            host["listener_config_path"], "listener_config_path"), lane_name=host["lane_name"],
        workspace_path=_absolute(host["workspace_path"], "workspace_path"),
        codex_home=_absolute(host["codex_home"], "codex_home"))


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

def _parse_time(value):
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None

def _run_ssh(command, source, timeout=30):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    stdout, stderr = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        process.stdin.write(source)
        process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectorError("ssh_timeout")
            events = selector.select(remaining)
            if not events:
                raise CollectorError("ssh_timeout")
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if len(key.data) > MAX_REMOTE_OUTPUT:
                    raise CollectorError("remote_output_invalid")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise CollectorError("ssh_failed")
        return bytes(stdout)
    except subprocess.TimeoutExpired:
        raise CollectorError("ssh_timeout") from None
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        process.stderr.close()

def _payload(config, collect_quota):
    return {"owner": config.owner, "listener_config_path": config.listener_config_path,
            "lane_name": config.lane_name, "workspace_path": config.workspace_path,
            "collect_quota": bool(collect_quota), "codex_home": config.codex_home}

def _checked(response):
    if not isinstance(response, dict) or response.get("ok") is not True:
        code = response.get("error") if isinstance(response, dict) else None
        raise CollectorError(code if code in SAFE_ERRORS else "remote_failed")
    return response

def collect_local(config, collect_quota):
    """Local mode: remote_sampler.py, loaded by path (this runs as a script and from the package), runs
    in this process with no SSH or sudo and answers as it does remotely."""
    sampler = runpy.run_path(str(Path(__file__).with_name("remote_sampler.py")))
    return _checked(sampler["respond"](_payload(config, collect_quota)))

def collect_remote(config, collect_quota):
    payload = _payload(config, collect_quota)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    remote_command = shlex.join(["sudo", "-n", "python3", "-", encoded])
    command = [*config.ssh_argv, config.destination, remote_command]
    source = Path(__file__).with_name("remote_sampler.py").read_bytes()
    try:
        response = json.loads(_run_ssh(command, source).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CollectorError("remote_output_invalid") from None
    return _checked(response)

def _number(value, minimum=0, maximum=None, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError
    return value


def _integer(value, minimum=0, maximum=None, nullable=False):
    value = _number(value, minimum, maximum, nullable)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError
    return value

def _state(value, choices=None):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value):
        raise ValueError
    if choices and value not in choices:
        raise ValueError
    return value


def project_host(raw, config):
    if not isinstance(raw, dict):
        raise ValueError
    cpu, memory, workspace = raw.get("cpu"), raw.get("memory"), raw.get("workspace")
    listener, limits, slots = raw.get("listener"), raw.get("lane_limits"), raw.get("slots")
    if not all(isinstance(item, dict) for item in (cpu, memory, workspace, listener, limits, slots)):
        raise ValueError
    limit = _integer(slots.get("limit"), 1, 64)
    qae = _integer(slots.get("qae_concurrency"), 0, limit)
    records, seen = [], set()
    raw_records = slots.get("occupied")
    if not isinstance(raw_records, list) or len(raw_records) > 2 * limit:
        raise ValueError
    for item in raw_records:
        if not isinstance(item, dict):
            raise ValueError
        kind = _state(item.get("kind"), ("ci", "qae"))
        index = _integer(item.get("index"), 1, limit)
        if (kind, index) in seen:
            raise ValueError
        seen.add((kind, index))
        unit = item.get("unit")
        if not isinstance(unit, dict):
            raise ValueError
        record = {"kind": kind, "index": index, "state": _state(
            item.get("state"), ("allocated", "unknown")), "unit": {
                "active_state": _state(unit.get("active_state")), "sub_state": _state(unit.get("sub_state"))}}
        if record["state"] == "allocated":
            match = TARGET_RE.fullmatch(item.get("target_repository", ""))
            if not match or match.group(1).casefold() != config.owner.casefold():
                raise ValueError
            if not TOKEN_RE.fullmatch(item.get("runner_name", "")) or not _parse_time(item.get("allocated_at")):
                raise ValueError
            record.update({"target_repository": config.owner + "/" + match.group(2),
                           "set_id": _integer(item.get("set_id"), 1),
                           "runner_id": _integer(item.get("runner_id"), 1),
                           "runner_name": item["runner_name"], "allocated_at": item["allocated_at"]})
        else:
            record["reason"] = _state(item.get("reason"), (
                "active-unit-without-job", "active_unit_without_job", "job_unreadable",
                "unit_state_unavailable"))
        records.append(record)
    started_at = listener.get("started_at")
    if started_at is not None and (not isinstance(started_at, str) or len(started_at) > 100
                                   or any(ord(char) < 32 for char in started_at)):
        raise ValueError
    body = {
        "label": config.host_label,
        "aggregate_scope": "shared_host",
        "cpu": {"busy_percent": _number(cpu.get("busy_percent"), 0, 100)},
        "memory": {"total_bytes": _integer(memory.get("total_bytes"), 1),
                   "available_bytes": _integer(memory.get("available_bytes"), 0)},
        "workspace": {"total_bytes": _integer(workspace.get("total_bytes"), 1),
                      "free_bytes": _integer(workspace.get("free_bytes"), 0)},
        "listener": {"state": _state(listener.get("state"), ("up", "down", "unknown")),
                     "active_state": _state(listener.get("active_state")),
                     "sub_state": _state(listener.get("sub_state")), "started_at": started_at},
        "lane_limits": {
            "memory_current_bytes": _integer(limits.get("memory_current_bytes"), 0, nullable=True),
            "memory_max_bytes": _integer(limits.get("memory_max_bytes"), 0, nullable=True),
            "memory_high_bytes": _integer(limits.get("memory_high_bytes"), 0, nullable=True),
            "cpu_usage_nsec": _integer(limits.get("cpu_usage_nsec"), 0, nullable=True),
            "cpu_quota_cores": _number(limits.get("cpu_quota_cores"), 0, nullable=True)},
        "slots": {"limit": limit, "qae_concurrency": qae, "occupied_count": len(records),
                  "remaining_on_demand": max(0, limit - len(records)), "occupied": records},
    }
    if body["memory"]["available_bytes"] > body["memory"]["total_bytes"]:
        raise ValueError
    if body["workspace"]["free_bytes"] > body["workspace"]["total_bytes"]:
        raise ValueError
    return body


def project_rate_limits(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise ValueError
    result = []
    for item in raw:
        if not isinstance(item, dict) or not TOKEN_RE.fullmatch(item.get("limit_id", "")):
            raise ValueError
        windows = []
        if not isinstance(item.get("windows"), list) or not 1 <= len(item["windows"]) <= 2:
            raise ValueError
        for window in item["windows"]:
            if not isinstance(window, dict):
                raise ValueError
            windows.append({"name": _state(window.get("name"), ("primary", "secondary")),
                            "duration_minutes": _integer(window.get("duration_minutes"), 1),
                            "used_percent": _number(window.get("used_percent"), 0, 100),
                            "resets_at": _integer(window.get("resets_at"), 1)})
        result.append({"limit_id": item["limit_id"], "windows": windows})
    return result


def _empty_host(config):
    return {"label": config.host_label, "aggregate_scope": "shared_host", "cpu": None,
            "memory": None, "workspace": None, "listener": None, "lane_limits": None,
            "slots": None}


def _previous_host(previous, config):
    try:
        host = previous["hosts"][0]
        body = project_host(host, config)
        observed = host.get("observed_at") if _parse_time(host.get("observed_at")) else None
        attempted = host.get("last_attempt_at") if _parse_time(host.get("last_attempt_at")) else observed
        return body, observed, attempted
    except (KeyError, IndexError, TypeError, ValueError):
        return _empty_host(config), None, None


def _previous_account(previous):
    try:
        account = previous["accounts"][0]
        limits = project_rate_limits(account["rate_limits"])
        observed = account.get("observed_at") if _parse_time(account.get("observed_at")) else None
        attempted = account.get("last_attempt_at") if _parse_time(account.get("last_attempt_at")) else observed
        return limits, observed, attempted
    except (KeyError, IndexError, TypeError, ValueError):
        return [], None, None


def load_previous(path, owner):
    try:
        previous = json.loads(_read_file(path, MAX_SNAPSHOT).decode("utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(previous, dict) or previous.get("version") != SCHEMA_VERSION:
        return {}
    if previous.get("owner") != owner:
        raise ValueError("output already belongs to a different owner")
    return previous


def _error_code(error):
    code = str(error)
    return code if code in SAFE_ERRORS else "collection_failed"


def _quota_due(last_attempt, now):
    attempted = _parse_time(last_attempt)
    current = _parse_time(now)
    return attempted is None or current is None or (current - attempted).total_seconds() >= QUOTA_INTERVAL_SECONDS


def _samples(previous, host, success, now):
    kept = []
    current = _parse_time(now)
    cutoff = current - dt.timedelta(seconds=HISTORY_SECONDS) if current else None
    for sample in previous.get("samples", []) if isinstance(previous, dict) else []:
        try:
            observed = _parse_time(sample.get("observed_at"))
            metrics = sample["host"]
            clean = {"observed_at": sample["observed_at"], "host": {
                "cpu_busy_percent": _number(metrics["cpu_busy_percent"], 0, 100),
                "memory_available_bytes": _integer(metrics["memory_available_bytes"], 0),
                "workspace_free_bytes": _integer(metrics["workspace_free_bytes"], 0),
                "slots_occupied": _integer(metrics["slots_occupied"], 0),
                "slots_remaining_on_demand": _integer(metrics["slots_remaining_on_demand"], 0)}}
            if observed and (cutoff is None or observed >= cutoff):
                kept.append(clean)
        except (KeyError, TypeError, ValueError):
            continue
    if success:
        kept.append({"observed_at": now, "host": {
            "cpu_busy_percent": host["cpu"]["busy_percent"],
            "memory_available_bytes": host["memory"]["available_bytes"],
            "workspace_free_bytes": host["workspace"]["free_bytes"],
            "slots_occupied": host["slots"]["occupied_count"],
            "slots_remaining_on_demand": host["slots"]["remaining_on_demand"]}})
    unique = {sample["observed_at"]: sample for sample in kept}
    return [unique[key] for key in sorted(unique)][-MAX_SAMPLES:]


def atomic_write(path, snapshot):
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def refresh(config, output, fetch=None, now=None):
    now = now or utc_now()
    previous = load_previous(output, config.owner)
    old_host, host_observed, host_attempted = _previous_host(previous, config)
    old_limits, account_observed, account_attempted = _previous_account(previous)
    due = _quota_due(account_attempted, now)
    response, fetch_error = None, None
    try:
        response = (fetch or (collect_remote if config.ssh_argv else collect_local))(config, due)
        host_body = project_host(response.get("host"), config)
        host_ok = True
    except (CollectorError, OSError, TypeError, ValueError) as error:
        host_body, host_ok, fetch_error = old_host, False, _error_code(error)
    host = {**host_body, "observed_at": now if host_ok else host_observed,
            "last_attempt_at": now, "available": host_ok, "stale": not host_ok,
            "error": None if host_ok else fetch_error}
    quota_ok, quota_error = False, None
    if due:
        try:
            quota = response.get("quota") if host_ok else None
            if not isinstance(quota, dict) or quota.get("status") != "ok":
                status = quota.get("status") if isinstance(quota, dict) else fetch_error
                raise CollectorError(status if status in SAFE_ERRORS else "quota_unavailable")
            limits = project_rate_limits(quota.get("rate_limits"))
            quota_ok = True
        except (CollectorError, TypeError, ValueError) as error:
            limits, quota_error = old_limits, _error_code(error)
        account_attempt = now
    else:
        limits, account_attempt = old_limits, account_attempted
        old_account = previous.get("accounts", [{}])[0] if previous.get("accounts") else {}
        quota_ok = bool(account_observed) and old_account.get("available") is True
        old_error = old_account.get("error")
        quota_error = old_error if old_error in SAFE_ERRORS else (None if quota_ok else "quota_unavailable")
    account = {"label": "Configured Codex account", "scope": "account_wide",
               "observed_at": now if quota_ok and due else account_observed,
               "last_attempt_at": account_attempt, "available": quota_ok,
               "stale": not quota_ok, "error": None if quota_ok else quota_error,
               "rate_limits": limits}
    snapshot = {"version": SCHEMA_VERSION, "owner": config.owner, "observed_at": now,
                "hosts": [host], "accounts": [account],
                "samples": _samples(previous, host_body, host_ok, now)}
    atomic_write(output, snapshot)
    return host_ok


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private collector configuration JSON")
    parser.add_argument("--output", required=True, help="local telemetry snapshot JSON")
    parser.add_argument("--interval", type=float, help="repeat every N seconds until interrupted")
    args = parser.parse_args(argv)
    if args.interval is not None and (not math.isfinite(args.interval) or args.interval <= 0):
        parser.error("--interval must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.interval is None:
        return 0 if refresh(config, args.output) else 1
    try:
        while True:
            started = time.monotonic()
            refresh(config, args.output)
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
