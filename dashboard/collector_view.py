"""Project the optional Linux collector into the dashboard's owner-scoped view."""
from datetime import timedelta
from types import SimpleNamespace

from .collector import project_host, project_rate_limits
from .util import parse_time, iso_time


def fresh(section, now):
    observed = parse_time(section.get("observed_at"))
    return (section.get("available") is True and section.get("stale") is False and
            observed is not None and timedelta(0) <= now - observed <= timedelta(minutes=5))


def collector_view(value, config, now):
    if (set(value) != {"version", "owner", "observed_at", "hosts", "accounts", "samples"} or
            value.get("version") != 1 or value.get("owner", "").lower() != config.owner.lower() or
            not isinstance(value.get("hosts"), list) or len(value["hosts"]) != 1 or
            not isinstance(value.get("accounts"), list) or len(value["accounts"]) != 1):
        raise ValueError("invalid collector identity")
    raw_host, account = value["hosts"][0], value["accounts"][0]
    if not isinstance(raw_host, dict) or not isinstance(account, dict):
        raise ValueError("invalid sections")
    capacity = {"available": False, "reason": "not_provided"}
    states = []
    if raw_host.get("observed_at"):
        host = project_host(raw_host, SimpleNamespace(owner=config.owner, host_label="Shared runner machine"))
        current = fresh(raw_host, now)
        lanes = []
        for row in host["slots"]["occupied"]:
            lane = {"id": "%s-%s" % (row["kind"], row["index"]),
                    "state": "allocated" if current and row["state"] == "allocated" else "unknown",
                    "registered": None, "labels": [row["kind"]]}
            if current and row["state"] == "allocated":
                lane["runner_id"] = row["runner_id"]
                lane["allocated_at"] = row["allocated_at"]
                lane["job"] = {"repository": row["target_repository"],
                               "name": "Runner allocated; job match pending", "url": None}
            lanes.append(lane)
        listener_up = host["listener"]["state"] == "up"
        for index in range(host["slots"]["remaining_on_demand"]):
            lanes.append({"id": "on-demand-%s" % (index + 1),
                          "state": "provisionable" if current and listener_up else "unknown",
                          "registered": False if current else None, "labels": []})
        capacity = {"available": True, "sampled_at": raw_host["observed_at"], "stale": not current,
                    "host": {"cpu_percent": host["cpu"]["busy_percent"],
                             "memory_used_bytes": host["memory"]["total_bytes"] - host["memory"]["available_bytes"],
                             "memory_total_bytes": host["memory"]["total_bytes"],
                             "workspace_disk_free_bytes": host["workspace"]["free_bytes"],
                             "workspace_disk_total_bytes": host["workspace"]["total_bytes"]},
                    "limits": {**host["lane_limits"], "slots": host["slots"]["limit"],
                               "qae_concurrency": host["slots"]["qae_concurrency"]},
                    "listener_state": host["listener"]["state"] if current else "unknown", "lanes": lanes}
        if current:
            qae_allocated = any(row["kind"] == "qae" for row in host["slots"]["occupied"])
            state = "down" if host["listener"]["state"] == "down" else "idle" if listener_up and not qae_allocated else "unknown"
            states.append({"owner": config.owner, "bot": "explorer", "state": state,
                           "detail": "Owner's QAE runner listener"})
    usage = {"available": False, "reason": "not_provided"}
    if account.get("observed_at"):
        limits = project_rate_limits(account.get("rate_limits"))
        windows = []
        from datetime import datetime, timezone
        for limit in limits:
            for window in limit["windows"]:
                minutes = window["duration_minutes"]
                duration = "%sd" % (minutes // 1440) if minutes % 1440 == 0 else "%sh" % (minutes / 60)
                windows.append({"name": "%s · %s" % (limit["limit_id"], duration),
                                "used_percent": window["used_percent"],
                                "resets_at": iso_time(datetime.fromtimestamp(window["resets_at"], timezone.utc)),
                                "allowance_tokens": None, "window_minutes": minutes})
        usage = {"available": True, "sampled_at": account["observed_at"], "stale": not fresh(account, now),
                 "accounts": [{"id": "collector:codex", "label": "Codex subscription", "provider": "OpenAI",
                               "quota_windows": windows}], "samples": [],
                 "completeness": "Subscription percentage is account-wide and may include other activity. Per-bot token history starts with captured runs."}
    return {"available": True, "capacity": capacity, "usage": usage,
            "bots": {"available": bool(states), "sampled_at": raw_host.get("observed_at"), "stale": not fresh(raw_host, now), "states": states}}


def join_runner_jobs(telemetry, github):
    """An allocation becomes busy only when GitHub confirms a matching active runner."""
    capacity = telemetry.get("capacity", {})
    if not capacity.get("available") or capacity.get("stale"):
        return
    jobs = {}
    for repository in github.get("repositories", []):
        if repository.get("stale") or repository.get("unavailable"):
            continue
        for pull in repository.get("pulls", []):
            for run in pull.get("runs", []):
                for job in run.get("jobs", []):
                    if job.get("runner_id") and job.get("status") == "in_progress":
                        jobs[(repository["repository"], job["runner_id"])] = job
                        jobs[(None, job["runner_id"])] = {**job, "repository": repository["repository"]}
    for lane in capacity.get("lanes", []):
        assignment = lane.get("job")
        job = jobs.get((assignment["repository"] if assignment else None, lane.get("runner_id")))
        if job and lane["state"] in ("allocated", "busy"):
            lane["state"] = "busy"
            lane["job"] = {"repository": assignment["repository"] if assignment else job["repository"],
                           "name": job["name"], "url": job.get("html_url")}
