"""Project explicitly configured identities, never infer them from job names."""
from datetime import timedelta

from .bot_runs import qae_instance
from .pace import plan_pace
from .util import parse_time


def _qae_concurrency(telemetry):
    if not telemetry.get("available"):
        return None
    capacity = telemetry.get("capacity") or {}
    if not capacity.get("available"):
        return None
    value = (capacity.get("limits") or {}).get("qae_concurrency")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _sample_instance(sample):
    number = sample.get("instance")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    return number


def _explorer_rows(agent, github, usage, concurrency):
    """One QAE card per lane instance. A run with no instance stays on the plain name."""
    source = github.get("bots", {}).get("roles", {}).get(agent.workflow_role, {})
    coverage = source.get("coverage") or {"history": "unavailable"}
    active = source.get("active") or []
    recent_2h = source.get("recent_2h") or []
    recent_7d = source.get("recent_7d") or []
    role_samples = [sample for sample in usage.get("samples", []) if sample.get("bot") == agent.workflow_role]
    numbers = set(range(1, concurrency + 1)) if concurrency else set()
    for row in (*active, *recent_2h, *recent_7d):
        number = qae_instance(row.get("runner_name"))
        if number:
            numbers.add(number)
    for sample in role_samples:
        number = _sample_instance(sample)
        if number:
            numbers.add(number)
    known = coverage.get("active") == "complete"

    def state_of(rows):
        if rows:
            return "working"
        return "idle" if known else "unknown"

    rows, taken = [], []
    for number in sorted(numbers):
        row_id = "%s-%s" % (agent.id, number)
        rows.append({"id": row_id, "name": "QAE %s" % number, "role": agent.role,
                     "state": state_of([row for row in active if qae_instance(row.get("runner_name")) == number]),
                     "recent_2h": [row for row in recent_2h if qae_instance(row.get("runner_name")) == number],
                     "recent_7d": [row for row in recent_7d if qae_instance(row.get("runner_name")) == number],
                     "coverage": coverage, "source": "CI workflow activity"})
        taken.extend({**sample, "bot": row_id} for sample in role_samples if _sample_instance(sample) == number)
    loose_active = [row for row in active if qae_instance(row.get("runner_name")) is None]
    loose_2h = [row for row in recent_2h if qae_instance(row.get("runner_name")) is None]
    loose_7d = [row for row in recent_7d if qae_instance(row.get("runner_name")) is None]
    loose_samples = [sample for sample in role_samples if _sample_instance(sample) is None]
    if loose_active or loose_2h or loose_7d or loose_samples:
        rows.append({"id": agent.id, "name": agent.name, "role": agent.role, "state": state_of(loose_active),
                     "recent_2h": loose_2h, "recent_7d": loose_7d, "coverage": coverage,
                     "source": "CI workflow activity"})
        taken.extend({**sample, "bot": agent.id} for sample in loose_samples)
    return rows, taken


def agent_view(config, github, telemetry, now):
    observed = telemetry.get("agents", {}) if telemetry.get("available") else {}
    runtime = {row["id"]: row for row in observed.get("rows", [])}
    usage = telemetry.get("usage", {}) if telemetry.get("available") else {}
    rows, samples = [], []
    concurrency = _qae_concurrency(telemetry)
    for agent in config.agents:
        if agent.workflow_role == "explorer":
            expanded, taken = _explorer_rows(agent, github, usage, concurrency)
            rows.extend(expanded)
            samples.extend(taken)
            continue
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
