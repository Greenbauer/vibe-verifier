// The CI timeline (docs/dashboard.md "CI timeline"): when each current-head job waited for a runner,
// ran and finished, on one time axis like a browser's network waterfall. A row shows one line per
// workflow; the expanded view shows every job and, on request, its steps. Read-only: it draws the
// times the collector already read and links out to GitHub.
(function () {
  "use strict";
  const { element: el, duration, link, checkState } = VV;
  // GitHub picks up a job within seconds when a runner is free, so a shorter wait is not drawn:
  // only a wait for a busy runner is.
  const WAIT_SHOWN = 30 * 1000;
  // GitHub creates a job that needs another the moment that one finishes. A finish this close to a
  // job's creation is read as the job it waited for.
  const LINK_SLACK = 5 * 1000;
  const MINI_HEIGHT = 32;
  // Drawing order on a shared line: waits underneath, failures on top so a red job is never hidden.
  const LAYER = { waiting: 0, success: 1, cancelled: 1, unknown: 1, running: 2, failed: 3 };
  const TICKS = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400];
  const openSteps = new Set();

  const at = value => { const time = Date.parse(value); return Number.isFinite(time) ? time : null; };
  const seconds = ms => duration(Math.max(0, ms) / 1000);

  // A status is a report another system posted, not work on a runner: a point at its last update,
  // never open and never waiting. A pending one is that system still working, drawn as running.
  function statusLane(row, group) {
    const when = at(row.completed_at) ?? at(row.started_at);
    if (when == null) return null;
    const state = checkState(row);
    return { id: `${group.key}/${row.id}`, name: row.name, kind: "status", group,
      state: state === "waiting" ? "running" : state, label: VV.STATUS_LABELS[row.category] || state,
      queued: when, started: when, ended: when, url: row.details_url, steps: [] };
  }

  // An Actions job (with its queue time and steps), or another app's check run (job is null).
  function workLane(row, group, job) {
    const state = checkState(row);
    const queued = at(job && job.created_at) ?? at(row.started_at);
    if (queued == null) return null;
    const open = state === "running" || state === "waiting";
    const started = state === "waiting" ? null : at(job ? job.started_at : row.started_at) ?? queued;
    const ended = open ? null : at(job ? job.completed_at : row.completed_at) ?? started ?? queued;
    return { id: `${group.key}/${row.id}`, name: row.name, kind: job ? "job" : "check", group, state, queued, started,
      ended: ended == null ? null : Math.max(ended, started ?? queued),
      url: (job && job.html_url) || row.details_url, steps: (job && job.steps) || [] };
  }

  // Every current-head check that has a time, as a lane: an Actions job (kind "job"), another app's
  // check run ("check"), or a commit status ("status"). Skipped checks never ran and are left out.
  function lanes(pull) {
    const jobs = new Map();
    (pull.runs || []).forEach(run => (run.jobs || []).forEach(job => jobs.set(job.id, { job, run })));
    const ran = row => checkState(row) !== "skipped";
    const work = (pull.checks || []).filter(ran).map(row => {
      const found = jobs.get(row.id);
      return found ? workLane(row, { key: `run-${found.run.id}`, name: found.run.name }, found.job)
        : workLane(row, { key: "checks", name: "Other checks" }, null);
    });
    const statuses = (pull.statuses || []).filter(ran).map(row => statusLane(row, { key: "statuses", name: "Commit statuses" }));
    return [...work, ...statuses].filter(Boolean);
  }

  // The chain of jobs that set the total time: from the last Actions job to finish, back through each
  // job whose finish created it. Only Actions jobs are created by another's finish, so other apps'
  // checks and statuses are never on the chain.
  function markCritical(all, now) {
    const work = all.filter(lane => lane.kind === "job");
    const end = lane => lane.ended ?? now;
    let current = work.reduce((best, lane) => (!best || end(lane) > end(best) ? lane : best), null);
    const seen = new Set();
    while (current && !seen.has(current)) {
      seen.add(current);
      current.critical = true;
      const from = current;
      current = work.filter(lane => lane !== from && lane.ended != null &&
          lane.ended <= from.queued + LINK_SLACK && lane.ended >= from.queued - LINK_SLACK)
        .sort((a, b) => (b.group.key === from.group.key) - (a.group.key === from.group.key) || b.ended - a.ended)[0];
    }
  }

  function extent(list, now) {
    if (!list.length) return null;
    const start = Math.min(...list.map(lane => lane.queued));
    return { start, span: Math.max(Math.max(...list.map(lane => lane.ended ?? now)) - start, 1000) };
  }

  // The head's lanes grouped by workflow, groups in the order they were first queued. Null when no
  // check on the head has a time yet. start and span cover every lane, for the opened view; row
  // covers only what the row draws (no statuses), or is null when that is nothing.
  function timeline(pull, now) {
    const all = lanes(pull);
    if (!all.length) return null;
    markCritical(all, now);
    const groups = new Map();
    all.slice().sort((a, b) => a.queued - b.queued || (a.started ?? a.queued) - (b.started ?? b.queued)).forEach(lane => {
      if (!groups.has(lane.group.key)) groups.set(lane.group.key, { ...lane.group, lanes: [] });
      groups.get(lane.group.key).lanes.push(lane);
    });
    return { ...extent(all, now), row: extent(all.filter(lane => lane.kind !== "status"), now),
      groups: [...groups.values()], lanes: all, running: all.some(lane => lane.ended == null) };
  }

  // The longest row timeline on screen, so every row is drawn on the same scale.
  function sharedScale(pulls, now) {
    const spans = pulls.map(pull => timeline(pull, now)?.row).filter(Boolean).map(row => row.span);
    return spans.length ? Math.max(...spans) : null;
  }

  const percent = (ms, width) => `${(Math.min(1, Math.max(0, ms / width)) * 100).toFixed(3)}%`;

  function bar(from, to, state, start, width, title) {
    return el("i", { class: `timeline-bar bar-${state}`, title,
      style: `left:${percent(from - start, width)};width:${percent(to - from, width)}` });
  }

  function waited(lane, now) {
    return (lane.started ?? now) - lane.queued;
  }

  function laneTitle(lane, now) {
    if (lane.kind === "status") return `${lane.name}: ${lane.label}`;
    const parts = [];
    const wait = waited(lane, now);
    if (lane.state === "waiting") parts.push(`waiting ${seconds(wait)} for a runner`);
    else if (wait > WAIT_SHOWN) parts.push(`waited ${seconds(wait)} for a runner`);
    if (lane.state === "running") parts.push(`running ${seconds(now - lane.started)}`);
    else if (lane.started != null && lane.ended > lane.started) parts.push(`ran ${seconds(lane.ended - lane.started)}`);
    return parts.length ? `${lane.name}: ${parts.join(", ")}` : lane.name;
  }

  function laneBars(lane, start, width, now) {
    const title = laneTitle(lane, now);
    const ran = lane.started ?? now;
    const parts = [];
    if (ran - lane.queued > WAIT_SHOWN) parts.push(bar(lane.queued, ran, "waiting", start, width, title));
    if (lane.started != null) parts.push(bar(lane.started, lane.ended ?? now, lane.state, start, width, title));
    return parts;
  }

  function summary(line, now) {
    const jobs = line.lanes.filter(lane => lane.kind !== "status");
    const longest = Math.max(0, ...jobs.map(lane => waited(lane, now)));
    const failed = jobs.filter(lane => lane.state === "failed").length;
    return [`CI timeline: ${jobs.length} ${jobs.length === 1 ? "check" : "checks"} over ${seconds(line.row.span)}`,
      longest > WAIT_SHOWN ? `longest runner wait ${seconds(longest)}` : null,
      failed ? `${failed} failed` : null].filter(Boolean).join(", ");
  }

  // The row's small timeline: one line per workflow, every job of it drawn on that line.
  // scale is the shared width in milliseconds, or null to fit this row.
  function mini(pull, now, scale) {
    const line = timeline(pull, now);
    if (!line) return null;
    const groups = line.groups.filter(group => group.key !== "statuses");
    if (!line.row) return null;
    const { start, span } = line.row;
    const width = Math.max(scale || 0, span);
    const gap = groups.length > 10 ? 0 : 1;
    const height = Math.max(1, Math.min(4, Math.floor((MINI_HEIGHT - gap * (groups.length - 1)) / groups.length)));
    const box = el("div", { class: "timeline-mini", role: "img", "aria-label": summary(line, now), style: `gap:${gap}px` });
    groups.forEach(group => box.append(el("div", { class: "timeline-line", style: `height:${height}px` },
      group.lanes.slice().sort((a, b) => LAYER[a.state] - LAYER[b.state])
        .flatMap(lane => laneBars(lane, start, width, now)))));
    if (line.running) box.append(el("i", { class: "timeline-now", style: `left:${percent(now - start, width)}` }));
    return box;
  }

  function tickLabel(value) {
    if (value < 60) return `${value}s`;
    if (value < 3600) return `${value / 60}m`;
    const hours = Math.floor(value / 3600), minutes = (value % 3600) / 60;
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  }

  // At most six ticks across the span, on round times.
  function ticks(span) {
    const total = span / 1000;
    const step = TICKS.find(value => total / value <= 6) || TICKS[TICKS.length - 1];
    const result = [];
    for (let value = 0; value <= total; value += step) result.push(value);
    return result;
  }

  function axis(width) {
    return el("div", { class: "timeline-row timeline-axis", "aria-hidden": "true" }, el("span", {}),
      el("div", { class: "timeline-track" }, ticks(width).map(value =>
        el("span", { class: "timeline-tick", style: `left:${percent(value * 1000, width)}` }, value ? tickLabel(value) : "0"))),
      el("span", {}), el("span", {}));
  }

  function laneText(lane, now) {
    if (lane.kind === "status") return "";
    if (lane.state === "waiting") return `waiting ${seconds(waited(lane, now))}`;
    if (lane.state === "running") return `${seconds(now - lane.started)} so far`;
    return lane.ended > lane.started ? seconds(lane.ended - lane.started) : "";
  }

  // A job's steps on the job's own scale: a one-minute job on an hour's axis would draw every step as
  // a dot. The note above them names that scale.
  function stepRows(lane, now) {
    const start = lane.started ?? lane.queued;
    const width = Math.max((lane.ended ?? now) - start, 1000);
    const rows = lane.steps.flatMap(step => {
      const from = at(step.started_at);
      const state = checkState(step);
      if (from == null || state === "skipped") return [];
      const to = at(step.completed_at) ?? (state === "running" ? now : from);
      return [el("div", { class: "timeline-row timeline-step" },
        el("span", { class: "timeline-name", title: step.name }, step.name),
        el("div", { class: "timeline-track" }, bar(from, Math.max(from, to), state, start, width, `${step.name}: ${seconds(to - from)}`)),
        el("span", { class: "timeline-time" }, state === "running" ? `${seconds(now - from)} so far` : seconds(to - from)),
        el("span", {}))];
    });
    return rows.length ? [el("div", { class: "timeline-step-scale" }, `Steps on this job's own ${seconds(width)} scale`), ...rows] : [];
  }

  // The expanded view under a row: an axis, then each workflow's jobs, each on its own line.
  // A job with steps opens them in place. options: owner (links only to that owner's GitHub
  // pages), key (this pull request), rerender (draws the list again after a toggle).
  function detail(pull, now, options) {
    const line = timeline(pull, now);
    const box = el("div", { class: "timeline-detail", id: options.id });
    if (!line) {
      box.append(el("p", { class: "muted" }, "No check on this head has a start time yet."));
      return box;
    }
    const width = line.span;
    box.append(axis(width));
    line.groups.forEach(group => {
      box.append(el("div", { class: "timeline-group" }, group.name));
      group.lanes.forEach(lane => {
        const stepKey = `${options.key}/${lane.id}`;
        const open = openSteps.has(stepKey);
        const name = lane.steps.length
          ? el("button", { type: "button", class: "timeline-name", "aria-expanded": String(open), "data-focus-key": `steps:${stepKey}`,
            title: `${open ? "Hide" : "Show"} the steps of ${lane.name}`,
            onclick: () => {
              if (open) openSteps.delete(stepKey); else openSteps.add(stepKey);
              options.rerender();
              document.querySelector(`[data-focus-key="${CSS.escape(`steps:${stepKey}`)}"]`)?.focus({ preventScroll: true });
            } },
            el("span", { class: "timeline-caret", "aria-hidden": "true" }, open ? "▾" : "▸"), lane.name)
          : el("span", { class: "timeline-name", title: lane.name }, lane.name);
        box.append(el("div", { class: `timeline-row${lane.critical ? " critical" : ""}` }, name,
          el("div", { class: "timeline-track" }, laneBars(lane, line.start, width, now)),
          el("span", { class: "timeline-time", title: laneTitle(lane, now) }, laneText(lane, now)),
          link("Log", lane.url, options.owner, "timeline-link")));
        if (open) box.append(...stepRows(lane, now));
      });
    });
    box.append(el("div", { class: "timeline-legend" },
      ["success", "failed", "running"].map(state => el("span", {}, el("i", { class: `bar-${state}` }), VV.STATUS_LABELS[state])),
      el("span", {}, el("i", { class: "bar-waiting" }), "Waiting for a runner (over 30s)"),
      el("span", {}, el("b", {}, "Bold"), " jobs set the total time")));
    return box;
  }

  const api = { lanes, timeline, sharedScale, mini, detail, ticks };
  if (typeof module === "object" && module.exports) module.exports = api;
  else window.VVTimeline = api;
})();
