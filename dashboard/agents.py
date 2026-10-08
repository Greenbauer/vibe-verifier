"""Project explicitly configured identities, never infer them from job names."""
from datetime import timedelta

from .bot_runs import qae_instance
from .pace import plan_pace
from .util import parse_time


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _qae_concurrency(telemetry):
    if not telemetry.get("available"):
        return None
    capacity = telemetry.get("capacity") or {}
    if not capacity.get("available"):
        return None
    value = (capacity.get("limits") or {}).get("qae_concurrency")
    return value if _positive_int(value) else None


def _sample_instance(sample):
    number = sample.get("instance")
    return number if _positive_int(number) else None


def _blank(agent):
    return {"id": agent.id, "name": agent.name, "role": agent.role, "state": "unknown",
            "recent_2h": [], "recent_7d": [], "coverage": {"history": "unavailable"}}


def _rows_named(rows, number):
    return [row for row in rows if qae_instance(row.get("runner_name")) == number]


def _rows_unnamed(rows):
    return [row for row in rows if qae_instance(row.get("runner_name")) is None]


def _runner_numbers(rows):
    found = set()
    for row in rows:
        number = qae_instance(row.get("runner_name"))
        if number:
            found.add(number)
    return found


def _sample_numbers(samples):
    found = set()
    for sample in samples:
        number = _sample_instance(sample)
        if number:
            found.add(number)
    return found


def _state(active, known):
    if active:
        return "working"
    return "idle" if known else "unknown"


def _activity(row_id, name, role, active, recent_2h, recent_7d, coverage, known):
    return {"id": row_id, "name": name, "role": role, "state": _state(active, known),
            "recent_2h": recent_2h, "recent_7d": recent_7d, "coverage": coverage,
            "source": "CI workflow activity"}


def _tagged(samples, row_id):
    return [{**sample, "bot": row_id} for sample in samples]


def _lists(source):
    return source.get("active") or [], source.get("recent_2h") or [], source.get("recent_7d") or []


def _role_samples(usage, role):
    return [sample for sample in usage.get("samples", []) if sample.get("bot") == role]


def _instance_numbers(active, recent_2h, recent_7d, samples, concurrency):
    numbers = set(range(1, concurrency + 1)) if concurrency else set()
    numbers.update(_runner_numbers((*active, *recent_2h, *recent_7d)))
    numbers.update(_sample_numbers(samples))
    return numbers


def _samples_for(samples, number):
    return [sample for sample in samples if _sample_instance(sample) == number]


def _instance_cards(agent, numbers, active, recent_2h, recent_7d, samples, coverage, known):
    rows, taken = [], []
    for number in sorted(numbers):
        row_id = "%s-%s" % (agent.id, number)
        rows.append(_activity(row_id, "QAE %s" % number, agent.role,
                              _rows_named(active, number), _rows_named(recent_2h, number),
                              _rows_named(recent_7d, number), coverage, known))
        taken.extend(_tagged(_samples_for(samples, number), row_id))
    return rows, taken


def _plain_card(agent, active, recent_2h, recent_7d, samples, coverage, known):
    loose_active, loose_2h, loose_7d = _rows_unnamed(active), _rows_unnamed(recent_2h), _rows_unnamed(recent_7d)
    loose_samples = [sample for sample in samples if _sample_instance(sample) is None]
    if not (loose_active or loose_2h or loose_7d or loose_samples):
        return None, []
    row = _activity(agent.id, agent.name, agent.role, loose_active, loose_2h, loose_7d, coverage, known)
    return row, _tagged(loose_samples, agent.id)


def _loose_samples(samples):
    return [sample for sample in samples if _sample_instance(sample) is None]


def _explorer_rows(agent, github, usage, concurrency):
    """One QAE card per reported lane instance. With no instance count, activity with no instance stays on the plain name."""
    source = github.get("bots", {}).get("roles", {}).get(agent.workflow_role, {})
    coverage = source.get("coverage") or {"history": "unavailable"}
    active, recent_2h, recent_7d = _lists(source)
    samples = _role_samples(usage, agent.workflow_role)
    numbers = _instance_numbers(active, recent_2h, recent_7d, samples, concurrency)
    known = coverage.get("active") == "complete"
    rows, taken = _instance_cards(agent, numbers, active, recent_2h, recent_7d, samples, coverage, known)
    # A reported instance count is the whole roster. Tokens with no instance stay in the samples
    # (bot left as the workflow role) so All bots can count them without a plain QAE card.
    if concurrency:
        taken.extend(_loose_samples(samples))
        return rows, taken
    plain, plain_taken = _plain_card(agent, active, recent_2h, recent_7d, samples, coverage, known)
    if plain:
        rows.append(plain)
        taken.extend(plain_taken)
    return rows, taken


def _workflow_state(source):
    if source.get("active"):
        return "working"
    if (source.get("coverage") or {}).get("active") == "complete":
        return "idle"
    return "unknown"


def _workflow_row(agent, github):
    source = github.get("bots", {}).get("roles", {}).get(agent.workflow_role, {})
    # A runner listener being down or available says nothing about an agent.
    row = _blank(agent)
    row.update(state=_workflow_state(source), recent_2h=source.get("recent_2h", []),
               recent_7d=source.get("recent_7d", []), coverage=source.get("coverage", row["coverage"]),
               source="CI workflow activity")
    return row


def _runs_within(history, now, hours):
    window = timedelta(hours=hours)
    return [run for run in history if timedelta(0) <= now - parse_time(run["completed_at"]) <= window][:5]


def _runtime_row(agent, source, observed, now):
    row = _blank(agent)
    row.update(state=source["state"], source="Agent runtime",
               coverage={"history": "stale" if observed.get("stale") else "complete"})
    history = sorted(source["runs"], key=lambda run: run["completed_at"], reverse=True)
    for key, hours in (("recent_2h", 2), ("recent_7d", 168)):
        row[key] = _runs_within(history, now, hours)
    return row


def _configured_row(agent, github, runtime, observed, now):
    if agent.workflow_role:
        return _workflow_row(agent, github)
    if agent.id in runtime:
        return _runtime_row(agent, runtime[agent.id], observed, now)
    return _blank(agent)


def _rows_for(agent, github, usage, runtime, observed, concurrency, now):
    if agent.workflow_role == "explorer":
        return _explorer_rows(agent, github, usage, concurrency)
    row = _configured_row(agent, github, runtime, observed, now)
    sample_key = agent.workflow_role or agent.id
    taken = _tagged(_role_samples(usage, sample_key), agent.id)
    return [row], taken


def agent_view(config, github, telemetry, now):
    observed = telemetry.get("agents", {}) if telemetry.get("available") else {}
    runtime = {row["id"]: row for row in observed.get("rows", [])}
    usage = telemetry.get("usage", {}) if telemetry.get("available") else {}
    rows, samples = [], []
    concurrency = _qae_concurrency(telemetry)
    for agent in config.agents:
        added, taken = _rows_for(agent, github, usage, runtime, observed, concurrency, now)
        rows.extend(added)
        samples.extend(taken)
    plotted = {**usage, "samples": samples}
    return {"rows": rows, "usage": {**plotted, "pace": plan_pace(plotted, now)}}
