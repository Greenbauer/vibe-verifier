// The Outcomes view (docs/dashboard.md "Outcomes view"): where merged pull requests' time went, and
// which checks held the runners. Read-only: it renders what the dashboard read from GitHub after each
// pull request merged (GET /api/outcomes), split with the CI timeline's own rules (VVTimeline.split).
(function () {
  "use strict";
  const { element: el, duration, formatTime, link, secondsClass } = VV;
  const node = typeof module === "object" && module.exports;
  const timeline = node ? require("./timeline.js") : window.VVTimeline;
  const trend = node ? require("./outcome-trend.js") : window.VVOutcomeTrend;
  const WEEK = 7 * 24 * 3600 * 1000;
  // Read again at most this often while the view is open; the server reads GitHub every five minutes.
  const REFETCH = 60 * 1000;
  const MAX_CHECKS = 50;
  const ERRORS = {
    github_budget_low: "Reading is paused while less than half of GitHub's hourly budget is left.",
  };
  const source = { view: null, fetched: 0, loading: false, failed: false };

  const at = value => Date.parse(value);

  // A merged pull request's three parts: waiting for a runner or QA slot and checks running (its
  // merged head's latest round), then waiting on the merge, from when it was merge-ready (checks done
  // and out of draft) to its merge.
  function outcome(record) {
    const runs = record.head_runs || [];
    const checks = runs.flatMap(run => run.jobs.map(job => ({ ...job, provider: "actions" })));
    const round = timeline.split({ runs, checks });
    const ready = Math.max(round ? round.ended : -Infinity, at(record.ready_at));
    return { record, round, merge_wait_seconds: Math.max(0, (at(record.merged_at) - ready) / 1000) };
  }

  function median(values) {
    const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
    if (!sorted.length) return null;
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }

  function measures(rows) {
    return {
      count: rows.length,
      wait: median(rows.map(row => row.round?.wait_seconds)),
      running: median(rows.map(row => row.round?.running_seconds)),
      merge: median(rows.map(row => row.merge_wait_seconds)),
    };
  }

  const shown = value => (value == null ? "None" : duration(value));

  // The colors the Pull requests view gives the same times: its CI time ("CI waiting", "CI took")
  // and its idle time after CI turn yellow after an hour and red after three.
  const tone = seconds => secondsClass(seconds, "ci");
  const timer = seconds => el("span", { class: tone(seconds) }, duration(seconds));

  function card(label, value, before, tone = null) {
    return el("div", { class: "capacity-card" }, el("span", { class: "muted" }, label), el("b", { class: tone }, value),
      el("small", { class: "muted" }, `Previous 7 days: ${before}`));
  }

  function cards(recent, before) {
    const timed = (label, kind) => card(`${label} (median)`, shown(recent[kind]), shown(before[kind]),
      recent[kind] == null ? null : tone(recent[kind]));
    return el("div", { class: "capacity-cards outcome-cards" },
      card("Merged pull requests", String(recent.count), String(before.count)),
      timed("Waiting for a runner", "wait"), timed("Checks running", "running"), timed("Waiting on the merge", "merge"));
  }

  // Runner time per check over the week's merged pull requests, largest first, with its share of the
  // total and how much of it was on self-hosted runners: the owner's own fixed pools.
  function checkRows(rows) {
    const totals = new Map();
    rows.forEach(row => (row.record.checks || []).forEach(check => {
      const total = totals.get(check.name) || { name: check.name, jobs: 0, seconds: 0, self_hosted_seconds: 0 };
      total.jobs += check.jobs; total.seconds += check.seconds; total.self_hosted_seconds += check.self_hosted_seconds;
      totals.set(check.name, total);
    }));
    return [...totals.values()].sort((a, b) => b.seconds - a.seconds || a.name.localeCompare(b.name));
  }

  function table(label, columns, rows, extra) {
    const header = el("div", { class: `outcome-row outcome-head muted ${extra}`, role: "row" },
      ...columns.map(name => el("span", { role: "columnheader" }, name)));
    return el("div", { class: "runner-table outcome-table", role: "table", "aria-label": label }, header, ...rows);
  }

  function checkTable(rows) {
    const checks = checkRows(rows);
    const all = checks.reduce((sum, check) => sum + check.seconds, 0);
    const body = checks.slice(0, MAX_CHECKS).map(check => el("div", { class: "outcome-row outcome-checks", role: "row" },
      el("span", { role: "cell", title: check.name }, check.name), el("span", { role: "cell" }, String(check.jobs)),
      el("span", { role: "cell" }, duration(check.seconds)),
      el("span", { role: "cell" }, duration(check.self_hosted_seconds)),
      el("span", { role: "cell" }, all ? `${Math.round(check.seconds / all * 100)}%` : "0%")));
    const more = checks.length > MAX_CHECKS ? el("p", { class: "muted runner-none" }, `${checks.length - MAX_CHECKS} more checks not shown.`) : null;
    return el("section", { class: "panel" }, el("div", { class: "panel-title" }, el("h2", {}, "RUNNER TIME BY CHECK"),
      el("span", { class: "muted" }, `Every run while open, last 7 days, ${duration(all)} in all`)),
    checks.length ? table("Runner time by check", ["Check", "Jobs", "Runner time", "On self-hosted", "Share"], body, "outcome-checks")
      : el("p", { class: "muted" }, "No job ran a runner."), more);
  }

  function pullTable(rows, owner) {
    const body = rows.map(row => el("div", { class: "outcome-row outcome-pulls", role: "row" },
      el("span", { role: "cell" }, link(`${row.record.repository.split("/").pop()}#${row.record.number}`, row.record.url, owner),
        el("small", { class: "muted outcome-title" }, row.record.title)),
      el("span", { role: "cell" }, formatTime(row.record.merged_at)),
      el("span", { role: "cell" }, row.round ? timer(row.round.wait_seconds) : "No CI"),
      el("span", { role: "cell" }, row.round ? timer(row.round.running_seconds) : "No CI"),
      el("span", { role: "cell" }, timer(row.merge_wait_seconds))));
    return el("section", { class: "panel" }, el("div", { class: "panel-title" }, el("h2", {}, "MERGED PULL REQUESTS"),
      el("span", { class: "muted" }, "Last 7 days, newest first")),
    table("Merged pull requests", ["Pull request", "Merged", "Waiting for a runner", "Checks running", "Waiting on the merge"],
      body, "outcome-pulls"));
  }

  function notice(view, sourceBanner) {
    const notes = [];
    if (source.failed) notes.push(sourceBanner("Could not refresh merged pull requests. Showing the last read.", "warning"));
    if (view.error) notes.push(sourceBanner(ERRORS[view.error] || `The last read of GitHub failed (${view.error}). Showing what was read before.`, "warning"));
    if (view.pending > 0) notes.push(sourceBanner(`Still reading ${view.pending} merged pull ${view.pending === 1 ? "request" : "requests"}; the numbers cover the ones read so far.`));
    return notes;
  }

  function draw(content, view, owner, { empty, sourceBanner, rerender }, now) {
    content.append(...notice(view, sourceBanner));
    const rows = view.pulls.map(outcome);
    const recent = rows.filter(row => now - at(row.record.merged_at) <= WEEK);
    const before = rows.filter(row => now - at(row.record.merged_at) > WEEK && now - at(row.record.merged_at) <= 2 * WEEK);
    content.append(cards(measures(recent), measures(before)), trend.panel(rows, now, median, rerender));
    if (!recent.length) { content.append(empty("No merged pull requests in the last 7 days", `Read ${formatTime(view.sampled_at)}.`)); return; }
    content.append(pullTable(recent, owner), checkTable(recent),
      el("p", { class: "muted coverage-note" }, `Read ${formatTime(view.sampled_at)}. Each pull request is read once after it merges.`));
  }

  async function fetchView(rerender) {
    source.loading = true;
    try {
      const response = await fetch("/api/outcomes", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("source unavailable");
      source.view = await response.json();
      source.failed = false;
    } catch (_) {
      source.failed = true;
    } finally {
      source.loading = false;
      source.fetched = Date.now();
      rerender();
    }
  }

  function render(content, snapshot, helpers) {
    const now = Date.now();
    if (!source.loading && now - source.fetched >= REFETCH) fetchView(helpers.rerender);
    content.replaceChildren(helpers.heading("Outcomes"));
    const view = source.view;
    if (!view) {
      content.append(source.failed ? helpers.empty("Outcomes unavailable", "The local read-only source could not be loaded.")
        : helpers.empty("Loading merged pull requests", "Reading what the dashboard kept."));
      return;
    }
    if (!view.available) {
      content.append(...notice(view, helpers.sourceBanner), helpers.empty("Merged pull requests are not read yet", "The first read runs in the background and takes a few minutes."));
      return;
    }
    draw(content, view, snapshot.owner, helpers, now);
  }

  const api = { render, outcome, measures, checkRows, median, source };
  if (typeof module === "object" && module.exports) module.exports = api;
  else window.VVOutcomes = api;
})();
