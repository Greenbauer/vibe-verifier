"""Project the optional Linux collector into the dashboard's owner-scoped view."""
from datetime import timedelta
from types import SimpleNamespace

from .collector import project_host, project_rate_limits
from .util import parse_time, iso_time


def _window_label(minutes):
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "1 day" if days == 1 else "%s days" % days
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else "%s hours" % hours
    return "%s min" % minutes


def _model_label(limit_id):
    if limit_id.isalpha():
        return limit_id[:1].upper() + limit_id[1:]
    return limit_id


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
                    "state": "allocated" if row["state"] == "allocated" else "unknown",
                    "registered": None, "labels": [row["kind"]]}
            if row["state"] == "allocated":
                lane["runner_id"] = row["runner_id"]
                lane["allocated_at"] = row["allocated_at"]
                if row.get("job_name") and row.get("job_repository"):
                    # The host recorded this job at start. It is busy whether or not an open pull
                    # request carries it. join_runner_jobs still replaces it when GitHub has a match.
                    lane["state"] = "busy"
                    lane["job"] = {"repository": row["job_repository"], "name": row["job_name"],
                                   "url": row.get("job_url")}
                elif row["target_repository"]:
                    # An organization-scope allocation names no repository; join_runner_jobs then finds
                    # the job by runner ID alone.
                    lane["job"] = {"repository": row["target_repository"],
                                   "name": "Runner allocated; job match pending", "url": None}
            lanes.append(lane)
        listener_up = host["listener"]["state"] == "up"
        for index in range(host["slots"]["remaining_on_demand"]):
            lanes.append({"id": "on-demand-%s" % (index + 1),
                          "state": "provisionable" if listener_up else "unknown",
                          "registered": False, "labels": []})
        capacity = {"available": True, "sampled_at": raw_host["observed_at"], "stale": not current,
                    "host": {"cpu_percent": host["cpu"]["busy_percent"],
                             "memory_used_bytes": host["memory"]["total_bytes"] - host["memory"]["available_bytes"],
                             "memory_total_bytes": host["memory"]["total_bytes"],
                             "workspace_disk_free_bytes": host["workspace"]["free_bytes"],
                             "workspace_disk_total_bytes": host["workspace"]["total_bytes"]},
                    "limits": {**host["lane_limits"], "slots": host["slots"]["limit"],
                               "qae_concurrency": host["slots"]["qae_concurrency"],
                               "wait_slots": host["slots"].get("wait_limit", 0)},
                    "listener_state": host["listener"]["state"], "lanes": lanes}
        qae_allocated = any(row["kind"] == "qae" for row in host["slots"]["occupied"])
        state = "down" if host["listener"]["state"] == "down" else "idle" if listener_up and not qae_allocated else "unknown"
        states.append({"owner": config.owner, "bot": "explorer", "state": state,
                       "detail": "Owner's QAE runner listener"})
    usage = {"available": False, "reason": "not_provided"}
    if account.get("observed_at"):
        limits = project_rate_limits(account.get("rate_limits"))
        from datetime import datetime, timezone
        accounts = []
        for limit in limits:
            windows = []
            for window in limit["windows"]:
                minutes = window["duration_minutes"]
                windows.append({"name": _window_label(minutes),
                                "used_percent": window["used_percent"],
                                "resets_at": iso_time(datetime.fromtimestamp(window["resets_at"], timezone.utc)),
                                "allowance_tokens": None, "window_minutes": minutes})
            # Each metered feature is its own plan. A Codex plan's 5-hour and 7-day windows stay together.
            accounts.append({"id": "collector:%s" % limit["limit_id"], "label": _model_label(limit["limit_id"]),
                             "provider": "OpenAI", "quota_windows": windows})
        usage = {"available": True, "sampled_at": account["observed_at"], "stale": not fresh(account, now),
                 "accounts": accounts, "samples": [],
                 "completeness": "Subscription percentage is account-wide and may include other activity. Per-bot token history starts with captured runs."}
    return {"available": True, "capacity": capacity, "usage": usage,
            "bots": {"available": bool(states), "sampled_at": raw_host.get("observed_at"), "stale": not fresh(raw_host, now), "states": states}}


def join_runner_jobs(telemetry, github):
    """An allocation becomes busy only when GitHub confirms a matching active runner."""
    capacity = telemetry.get("capacity", {})
    if not capacity.get("available"):
        return
    jobs = {}
    for repository in github.get("repositories", []):
        if repository.get("unavailable"):
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
