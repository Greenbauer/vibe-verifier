"""Project explicitly configured identities, never infer them from job names."""
from datetime import timedelta

from .pace import plan_pace
from .util import parse_time


def agent_view(config, github, telemetry, now):
    observed = telemetry.get("agents", {}) if telemetry.get("available") else {}
    runtime = {row["id"]: row for row in observed.get("rows", [])}
    usage = telemetry.get("usage", {}) if telemetry.get("available") else {}
    rows, samples = [], []
    for agent in config.agents:
        row = {"id": agent.id, "name": agent.name, "role": agent.role, "state": "unknown",
               "recent_2h": [], "recent_7d": [], "coverage": {"history": "unavailable"}}
        if agent.workflow_role:
            source = github.get("bots", {}).get("roles", {}).get(agent.workflow_role, {})
            # A runner listener being down or available says nothing about an agent.
            row.update(state="working" if source.get("active") else
                       "idle" if source.get("coverage", {}).get("active") == "complete" else "unknown",
                       recent_2h=source.get("recent_2h", []), recent_7d=source.get("recent_7d", []),
                       coverage=source.get("coverage", row["coverage"]), source="CI workflow activity")
        elif agent.id in runtime:
            source = runtime[agent.id]
            row.update(state=source["state"], source="Agent runtime",
                       coverage={"history": "stale" if observed.get("stale") else "complete"})
            history = sorted(source["runs"], key=lambda run: run["completed_at"], reverse=True)
            for key, hours in (("recent_2h", 2), ("recent_7d", 168)):
                row[key] = [run for run in history if timedelta(0) <= now - parse_time(run["completed_at"]) <= timedelta(hours=hours)][:5]
        rows.append(row)
        sample_key = agent.workflow_role or agent.id
        samples.extend({**sample, "bot": agent.id} for sample in usage.get("samples", [])
                       if sample["bot"] == sample_key)
    plotted = {**usage, "samples": samples}
    return {"rows": rows, "usage": {**plotted, "pace": plan_pace(plotted, now)}}
