#!/usr/bin/env python3
"""Fixed, read-only Linux sampler executed by collector.py over SSH."""

import base64
import json
import math
import os
import re
import selectors
import stat
import subprocess
import sys
import time
import urllib.request
from urllib.error import HTTPError
from http.client import HTTPException

MAX_FILE = 64 * 1024
MAX_JOB = 4096
MAX_PROCESS_OUTPUT = 256 * 1024
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})$")
RUNNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class SampleError(Exception):
    """An error code safe to return to the local collector."""


class QuotaError(Exception):
    """A quota read failure safe to return without credentials or response text."""


def read_limited(path, limit=MAX_FILE):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise OSError("file is too large")
    finally:
        os.close(fd)
    return b"".join(chunks)


def listener_contract(path, owner, lane):
    try:
        config = json.loads(read_limited(path).decode("utf-8"))
        url_owner = config["github_url"].removeprefix("https://github.com/")
        budget = config["budget"]
        slots = budget["slots"]
        qae = budget["qae_concurrency"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise SampleError("listener_config_unavailable") from None
    if config.get("name") != lane or not OWNER_RE.fullmatch(url_owner):
        raise SampleError("listener_identity_mismatch")
    if url_owner.casefold() != owner.casefold():
        raise SampleError("listener_identity_mismatch")
    if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= 64:
        raise SampleError("listener_budget_invalid")
    if not isinstance(qae, int) or isinstance(qae, bool) or not 0 <= qae <= slots:
        raise SampleError("listener_budget_invalid")
    return slots, qae


def parse_cpu_line(text):
    first = text.splitlines()[0].split()
    if len(first) < 9 or first[0] != "cpu":
        raise SampleError("cpu_unavailable")
    try:
        # guest and guest_nice are already included in user and nice.
        values = [int(value) for value in first[1:9]]
    except ValueError:
        raise SampleError("cpu_unavailable") from None
    idle = values[3] + values[4]
    return sum(values), idle


def cpu_busy_percent(before, after):
    total_before, idle_before = parse_cpu_line(before)
    total_after, idle_after = parse_cpu_line(after)
    total_delta = total_after - total_before
    idle_delta = idle_after - idle_before
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        raise SampleError("cpu_unavailable")
    return round(100.0 * (total_delta - idle_delta) / total_delta, 2)


def parse_memory(text):
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] in ("MemTotal:", "MemAvailable:") and fields[2] == "kB":
            try:
                values[fields[0]] = int(fields[1]) * 1024
            except ValueError:
                raise SampleError("memory_unavailable") from None
    if set(values) != {"MemTotal:", "MemAvailable:"}:
        raise SampleError("memory_unavailable")
    return values["MemTotal:"], values["MemAvailable:"]


def run_bounded(command, timeout=5, limit=MAX_PROCESS_OUTPUT):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            events = selector.select(remaining)
            if not events:
                raise TimeoutError
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > limit:
                    raise OSError("command output is too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        returncode = process.wait(timeout=remaining)
    except (TimeoutError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise TimeoutError from None
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
    if returncode != 0:
        raise OSError("command failed")
    return bytes(output)


def parse_properties(data):
    result = {}
    for line in data.decode("utf-8", "strict").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def systemd_properties(unit, names, runner=run_bounded):
    command = ["systemctl", "show", "--no-pager"]
    for name in names:
        command.extend(["--property", name])
    command.append(unit)
    try:
        return parse_properties(runner(command, timeout=5, limit=MAX_FILE))
    except (OSError, TimeoutError, UnicodeError):
        return None


def slot_units(lane, slots, kinds=("ci", "qae")):
    return ["%s-%s@%d.service" % (lane, kind, index)
            for kind in kinds for index in range(1, slots + 1)]


def unit_states(lane, slots, runner=run_bounded, kinds=("ci", "qae")):
    units = slot_units(lane, slots, kinds)
    command = ["systemctl", "show", "--no-pager", "--property", "Id",
               "--property", "ActiveState", "--property", "SubState", *units]
    try:
        text = runner(command, timeout=5, limit=MAX_FILE).decode("utf-8", "strict")
    except (OSError, TimeoutError, UnicodeError):
        return {unit: None for unit in units}
    found = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        props = parse_properties(block.encode())
        if props.get("Id") in units:
            found[props["Id"]] = props
    return {unit: found.get(unit) for unit in units}


def safe_state(value):
    return value if isinstance(value, str) and STATE_RE.fullmatch(value) else "unknown"


def read_job(path, owner, expected_kind):
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        return {"error": "job_unreadable"}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return {"error": "job_unreadable"}
        data = os.read(fd, MAX_JOB + 1)
    except OSError:
        return {"error": "job_unreadable"}
    finally:
        os.close(fd)
    if len(data) > MAX_JOB:
        return {"error": "job_unreadable"}
    try:
        fields = data.decode("utf-8").split()
        if len(fields) != 5 or fields[1] != expected_kind:
            raise ValueError
        match = TARGET_RE.fullmatch(fields[0])
        set_id, runner_id = int(fields[2]), int(fields[3])
        if not match or set_id <= 0 or runner_id <= 0 or not RUNNER_RE.fullmatch(fields[4]):
            raise ValueError
    except (UnicodeError, ValueError):
        return {"error": "job_unreadable"}
    if match.group(1).casefold() != owner.casefold():
        raise SampleError("foreign_job_target")
    return {
        "target_repository": owner + "/" + match.group(2),
        "set_id": set_id,
        "runner_id": runner_id,
        "runner_name": fields[4],
        "allocated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime)),
    }


def scan_slots(owner, lane, slots, states, run_root="/run", kinds=("ci", "qae")):
    occupied = []
    for kind in kinds:
        for index in range(1, slots + 1):
            unit = "%s-%s@%d.service" % (lane, kind, index)
            props = states.get(unit)
            active = safe_state(props.get("ActiveState")) if props else "unknown"
            sub = safe_state(props.get("SubState")) if props else "unknown"
            job = read_job(os.path.join(run_root, lane, kind, str(index), "job"), owner, kind)
            if job is None and active in ("inactive", "failed"):
                continue
            record = {"kind": kind, "index": index,
                      "unit": {"active_state": active, "sub_state": sub}}
            if job and "error" not in job:
                record.update(job)
                record["state"] = "allocated"
            else:
                record["state"] = "unknown"
                record["reason"] = (job or {}).get(
                    "error", "unit_state_unavailable" if active == "unknown" else "active_unit_without_job")
            occupied.append(record)
    return occupied


def parse_systemd_number(value):
    if not isinstance(value, str) or value in ("", "infinity", "[not set]"):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def parse_quota_cores(value):
    if not isinstance(value, str) or value == "infinity":
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(us|ms|s|min)?", value)
    if not match:
        return None
    multipliers = {None: 0.000001, "us": 0.000001, "ms": 0.001, "s": 1.0, "min": 60.0}
    result = float(match.group(1)) * multipliers[match.group(2)]
    return round(result, 6) if math.isfinite(result) else None


# Matched to Codex rust-v0.159.2 backend-client/src/client/rate_limit_resets.rs.
# This internal route is version-coupled; it must fail unavailable if its contract changes.
QUOTA_URL = "https://chatgpt.com/backend-api/wham/usage"
QUOTA_TIMEOUT_SECONDS = 10
MAX_QUOTA_BODY = 256 * 1024


def normalize_rate_limits(payload):
    if not isinstance(payload, dict):
        raise QuotaError("quota_malformed")
    additional = payload.get("additional_rate_limits") or []
    if not isinstance(additional, list) or len(additional) > 31:
        raise QuotaError("quota_malformed")
    buckets = [("codex", payload.get("rate_limit"))]
    for item in additional:
        if not isinstance(item, dict):
            raise QuotaError("quota_malformed")
        buckets.append((item.get("metered_feature"), item.get("rate_limit")))
    limits = []
    for key, item in buckets:
        if not isinstance(key, str) or not RUNNER_RE.fullmatch(key):
            raise QuotaError("quota_malformed")
        if item is None:
            continue
        if not isinstance(item, dict):
            raise QuotaError("quota_malformed")
        windows = []
        for name in ("primary", "secondary"):
            window = item.get(name + "_window")
            if window is None:
                continue
            if not isinstance(window, dict):
                raise QuotaError("quota_malformed")
            used, seconds, reset = (window.get("used_percent"),
                                   window.get("limit_window_seconds"), window.get("reset_at"))
            if (not isinstance(used, (int, float)) or isinstance(used, bool)
                    or not math.isfinite(used) or not 0 <= used <= 100
                    or not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0
                    or not isinstance(reset, int) or isinstance(reset, bool) or reset <= 0):
                raise QuotaError("quota_malformed")
            # Same ceiling conversion as Codex's window_minutes_from_seconds().
            windows.append({"name": name, "duration_minutes": (seconds + 59) // 60,
                            "used_percent": used, "resets_at": reset})
        if windows:
            limits.append({"limit_id": key, "windows": windows})
    if not limits:
        raise QuotaError("quota_malformed")
    return limits


class NoQuotaRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def quota_read(codex_home, opener=None):
    """Read existing managed auth once in memory; never refresh or write its store."""
    try:
        auth = json.loads(read_limited(os.path.join(codex_home, "auth.json")))
        if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt":
            raise ValueError
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError
        token, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(token, str) or not token:
            raise ValueError
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
                   "User-Agent": "ci-dashboard-collector/1"}
        if account is not None:
            if not isinstance(account, str) or not account:
                raise ValueError
            headers["ChatGPT-Account-ID"] = account
        request = urllib.request.Request(QUOTA_URL, headers=headers, method="GET")
        opener = opener or urllib.request.build_opener(NoQuotaRedirect())
        with opener.open(request, timeout=QUOTA_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_QUOTA_BODY + 1)
        if len(body) > MAX_QUOTA_BODY:
            raise ValueError
        return normalize_rate_limits(json.loads(body))
    except HTTPError as error:
        error.close()
        raise QuotaError("quota_unavailable") from None
    except (OSError, ValueError, TypeError, HTTPException):
        raise QuotaError("quota_unavailable") from None


def collect(payload, run_root="/run", runner=run_bounded, sleeper=time.sleep,
            statvfs=os.statvfs, quota_reader=quota_read):
    owner, lane = payload.get("owner"), payload.get("lane_name")
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        raise SampleError("invalid_arguments")
    if not isinstance(lane, str) or not LANE_RE.fullmatch(lane):
        raise SampleError("invalid_arguments")
    slots, qae = listener_contract(payload.get("listener_config_path"), owner, lane)
    try:
        before = read_limited("/proc/stat").decode()
        sleeper(0.2)
        after = read_limited("/proc/stat").decode()
        memory = parse_memory(read_limited("/proc/meminfo").decode())
        disk = statvfs(payload.get("workspace_path"))
    except (OSError, UnicodeError, TypeError):
        raise SampleError("host_metrics_unavailable") from None
    states = unit_states(lane, slots, runner)
    occupied = scan_slots(owner, lane, slots, states, run_root)
    listener = systemd_properties(lane + "-listener.service",
                                  ("ActiveState", "SubState", "ExecMainStartTimestamp"), runner)
    slice_props = systemd_properties(lane + ".slice", ("MemoryCurrent", "MemoryMax", "MemoryHigh",
                                      "CPUUsageNSec", "CPUQuotaPerSecUSec"), runner)
    listener_active = safe_state(listener.get("ActiveState")) if listener else "unknown"
    host = {
        "cpu": {"busy_percent": cpu_busy_percent(before, after)},
        "memory": {"total_bytes": memory[0], "available_bytes": memory[1]},
        "workspace": {"total_bytes": disk.f_blocks * disk.f_frsize,
                      "free_bytes": disk.f_bavail * disk.f_frsize},
        "listener": {"state": "up" if listener_active == "active" else
                     "down" if listener_active in ("inactive", "failed") else "unknown",
                     "active_state": listener_active,
                     "sub_state": safe_state(listener.get("SubState")) if listener else "unknown",
                     "started_at": listener.get("ExecMainStartTimestamp") or None if listener else None},
        "lane_limits": {"memory_current_bytes": parse_systemd_number((slice_props or {}).get("MemoryCurrent")),
                        "memory_max_bytes": parse_systemd_number((slice_props or {}).get("MemoryMax")),
                        "memory_high_bytes": parse_systemd_number((slice_props or {}).get("MemoryHigh")),
                        "cpu_usage_nsec": parse_systemd_number((slice_props or {}).get("CPUUsageNSec")),
                        "cpu_quota_cores": parse_quota_cores((slice_props or {}).get("CPUQuotaPerSecUSec"))},
        "slots": {"limit": slots, "qae_concurrency": qae, "occupied": occupied},
    }
    quota = {"status": "not_requested"}
    if payload.get("collect_quota"):
        try:
            limits = quota_reader(payload["codex_home"])
            quota = {"status": "ok", "rate_limits": limits}
        except (KeyError, TypeError, QuotaError):
            quota = {"status": "quota_unavailable"}
    return {"ok": True, "host": host, "quota": quota}


def respond(payload):
    """collect() as the collector reads it, over SSH or in-process (its local mode): the sample, or
    an error code and never raw text."""
    try:
        if not isinstance(payload, dict):
            raise SampleError("invalid_arguments")
        return collect(payload)
    except SampleError as error:
        return {"ok": False, "error": str(error)}
    except Exception:
        return {"ok": False, "error": "collection_failed"}


def main():
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > MAX_FILE * 2:
            raise SampleError("invalid_arguments")
        raw = base64.urlsafe_b64decode(sys.argv[1].encode())
        result = respond(json.loads(raw.decode("utf-8")))
    except SampleError as error:
        result = {"ok": False, "error": str(error)}
    except Exception:
        result = {"ok": False, "error": "collection_failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
