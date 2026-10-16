// The Capacity view (docs/dashboard.md "Capacity view"): the shared machine, what each runner is doing
// now, and the lane's last hour, day or week. Read-only: it renders what the collector recorded.
(function () {
  "use strict";
  const { element: el, formatTime, bytes, diskUsage, duration, link } = VV;
  const RANGE_LABELS = { "1h": "Last hour", "24h": "Last 24 hours", "7d": "Last 7 days" };
  const STATE_TEXT = { busy: "Running", allocated: "Idle, no job", warm: "Kept warm", unknown: "Unknown", offline: "Offline", ready: "Ready" };
  // The machine's three kinds of runner slot, in the words the page uses for them.
  const KINDS = {
    ci: { title: "Build and test", short: "Build", note: "Runs the repositories' CI jobs." },
    qae: { title: "AI QA", short: "QA", note: "Runs the QA explorer against a preview. Each slot has its own Codex login." },
    wait: { title: "Waiting jobs", short: "Wait", note: "Holds jobs that only wait, such as for a preview to deploy. Cheap, and not one of the runner slots." }
  };
  let chosenRange = "24h";

  const seconds = (from, now) => {
    const start = Date.parse(from);
    return Number.isFinite(start) ? Math.max(0, (now - start) / 1000) : null;
  };
  const kindOf = lane => lane.labels[0] || lane.id.replace(/-\d+$/, "");
  // "ci-3" is slot 3 of the build-and-test kind, "Build 3": the service <lane>-ci@3 on the runner
  // machine. Each kind numbers its own slots, so the kind is part of the name.
  const slotName = lane => `${KINDS[kindOf(lane)]?.short || kindOf(lane)} ${lane.id.match(/-(\d+)$/)?.[1] ?? lane.id}`;
  const unitName = lane => lane.id.replace(/-(\d+)$/, "@$1");
  const isFree = lane => lane.state === "provisionable";

  function metric(label, value, percent) {
    const row = el("div", { class: "host-metric" }, el("span", {}, label), el("b", {}, value));
    if (Number.isFinite(percent)) row.append(el("div", { class: "metric-meter", role: "meter", "aria-valuenow": percent,
      "aria-valuemin": 0, "aria-valuemax": 100 }, el("span", { style: `width:${percent}%` })));
    return row;
  }

  function hostPanel(capacity) {
    const host = capacity.host || {};
    const memoryPercent = Number.isFinite(host.memory_used_bytes) && host.memory_total_bytes ? host.memory_used_bytes / host.memory_total_bytes * 100 : null;
    const disk = diskUsage(host);
    return el("section", { class: "panel host-panel" }, el("div", { class: "panel-title" }, el("h2", {}, "SHARED HOST"),
      el("span", { class: "muted" }, `Sampled ${formatTime(capacity.sampled_at)}`)), el("div", { class: "host-grid" },
      metric("CPU sampled", Number.isFinite(host.cpu_percent) ? `${host.cpu_percent}%` : "Unavailable", host.cpu_percent),
      metric("Memory", Number.isFinite(host.memory_used_bytes) ? `${bytes(host.memory_used_bytes)} / ${bytes(host.memory_total_bytes)}` : "Unavailable", memoryPercent),
      metric("Workspace disk", disk ? `${bytes(disk.used)} / ${bytes(disk.total)}` : "Unavailable", disk?.percent)));
  }

  // One plain sentence for the lane's limits: what "8 slots, QAE maximum 4" means in practice.
  function limitsLine(limits) {
    if (!limits) return null;
    const parts = [`Up to ${limits.slots} runners at once`];
    if (limits.qae_concurrency) parts[0] += ` (AI QA at most ${limits.qae_concurrency})`;
    if (limits.wait_slots) parts.push(`${limits.wait_slots} more for jobs that only wait`);
    if (Number.isFinite(limits.cpu_quota_cores)) parts.push(`${limits.cpu_quota_cores} CPU cores`);
    if (Number.isFinite(limits.memory_max_bytes)) parts.push(`${bytes(limits.memory_max_bytes)} memory`);
    return el("p", { class: "muted lane-limits" }, parts.join(" · "));
  }

  function card(label, value, detail) {
    return el("div", { class: "capacity-card" }, el("span", { class: "muted" }, label), el("b", {}, value),
      detail ? el("small", { class: "muted" }, detail) : null);
  }

  // The typical wait, from GitHub handing a job to the lane until a runner takes it, as the value;
  // the slowest 5% as the detail, and GitHub's own routing before that when it reported it.
  function waitText(wait, label) {
    if (!wait) return ["No jobs", label];
    const routing = wait.routing_seconds == null ? "" :
      ` Before that, GitHub took ${duration(wait.routing_seconds)} on average to hand it to the lane.`;
    return [`${duration(wait.p50_seconds)} typical`,
      `${label}: ${duration(wait.p95_seconds)} for the slowest 5% of ${wait.jobs} jobs.${routing}`];
  }

  function summary(capacity, range) {
    const shared = capacity.lanes.filter(lane => kindOf(lane) !== "wait");
    const limit = capacity.limits?.slots ?? shared.length;
    const inUse = shared.filter(lane => !isFree(lane) && KINDS[kindOf(lane)]).length;
    const history = capacity.history;
    const waiting = history?.waiting_now;
    const label = RANGE_LABELS[range?.name] || "";
    const [wait, waitDetail] = waitText(range?.wait, label);
    return el("div", { class: "capacity-cards" },
      card("Runners in use now", `${inUse} of ${limit}`, history?.warm ? `Warm pool: ${history.warm} kept ready for the next job` : null),
      card("Jobs waiting for a runner", Number.isInteger(waiting) ? String(waiting) : "Unknown", "Right now"),
      card("Busy share", Number.isFinite(range?.busy_percent) ? `${range.busy_percent}%` : "No data",
        `${label}: share of the ${limit} runner slots running a job`),
      card("Wait for a runner", wait, waitDetail),
      card("Idle runners", Number.isFinite(range?.idle_hours) ? duration(range.idle_hours * 3600) : "No data",
        `${label}: runners up with no job` + (range?.warm_hours ? `, not counting ${duration(range.warm_hours * 3600)} kept warm on purpose` : "")));
  }

  function bar(bucket, limit, step) {
    const start = formatTime(bucket.start);
    if (!("running" in bucket)) return el("div", { class: "capacity-bar no-data", title: `${start}: no samples` });
    const pct = value => `${Math.min(100, value / limit * 100)}%`;
    const warm = bucket.warm || 0;
    const free = Math.max(0, limit - bucket.running - warm - bucket.reserved);
    const tip = `${start}, ${duration(step)}: average ${bucket.running} running, ${warm} kept warm, ${bucket.reserved} idle, ` +
      `${free.toFixed(1)} free` + (bucket.waiting ? `; up to ${bucket.waiting} jobs waiting` : "");
    return el("div", { class: "capacity-bar", title: tip },
      bucket.waiting ? el("span", { class: "waiting-mark" }) : null,
      el("span", { class: "bar-reserved", style: `height:${pct(bucket.reserved)}` }),
      el("span", { class: "bar-warm", style: `height:${pct(warm)}` }),
      el("span", { class: "bar-running", style: `height:${pct(bucket.running)}` }));
  }

  function chart(range, limit, rerender) {
    const buttons = el("div", { class: "range-buttons", role: "group", "aria-label": "Time range" },
      ...Object.keys(RANGE_LABELS).map(name => el("button", { class: name === range.name ? "active" : null,
        "aria-pressed": name === range.name ? "true" : "false", onclick: () => { chosenRange = name; rerender(); } }, name)));
    const bars = el("div", { class: "capacity-chart", role: "img",
      "aria-label": `Runners over the ${RANGE_LABELS[range.name].toLowerCase()}: running, kept warm, idle and free out of ${limit}` },
      ...range.buckets.map(bucket => bar(bucket, limit, range.bucket_seconds)));
    const first = range.buckets[0]?.start;
    const axis = el("div", { class: "capacity-axis muted" }, el("span", {}, formatTime(first)), el("span", {}, "now"));
    const legend = el("div", { class: "capacity-legend muted" },
      el("span", {}, el("i", { class: "swatch bar-running" }), "Running a job"),
      el("span", {}, el("i", { class: "swatch bar-warm" }), "Kept warm"),
      el("span", {}, el("i", { class: "swatch bar-reserved" }), "Idle, no job"),
      el("span", {}, el("i", { class: "swatch free" }), `Free (of ${limit})`),
      el("span", {}, el("i", { class: "swatch waiting-mark" }), "Jobs waited"),
      el("span", {}, el("i", { class: "swatch no-data" }), "No samples"));
    return el("div", { class: "capacity-history" },
      el("div", { class: "panel-title" }, el("h2", {}, "Runners over time"), buttons), bars, axis, legend);
  }

  function jobCell(lane, state, owner) {
    if (state === "busy" && lane.job) return el("span", {}, link(lane.job.name, lane.job.url, owner) || lane.job.name);
    if (state === "warm") return el("span", { class: "muted" }, "Ready for the next job; kept up on purpose");
    if (state === "allocated") return el("span", { class: "muted" }, "Waiting for GitHub to send a job");
    return el("span", { class: "muted" }, "No job recorded");
  }

  function wherePart(lane, state) {
    if (!lane.job || state !== "busy") return "";
    const repo = lane.job.repository.split("/")[1];
    return Number.isInteger(lane.job.pull) ? `${repo} #${lane.job.pull}` : repo;
  }

  function timeCell(lane, state, now) {
    const value = seconds(state === "busy" ? (lane.started_at || lane.allocated_at) : lane.allocated_at, now);
    if (value == null) return "";
    return state === "busy" ? duration(value) : `${duration(value)} idle`;
  }

  function runnerRow(lane, state, owner, now) {
    return el("div", { class: `runner-row state-${state}`, role: "row" },
      el("span", { class: "mono", role: "cell", title: `Service ${unitName(lane)} on the runner machine` }, slotName(lane)),
      el("span", { class: "runner-state", role: "cell" }, STATE_TEXT[state] || state),
      el("span", { role: "cell" }, jobCell(lane, state, owner)),
      el("span", { class: "muted", role: "cell" }, wherePart(lane, state)),
      el("span", { class: "mono", role: "cell" }, timeCell(lane, state, now)));
  }

  // The warm pool keeps a few runners up with no job on purpose. The listener names them
  // (queue.json warm_instances, from the same order it stops idle runners in); a runner it names that
  // has no job reads "Kept warm". Without that list (an older listener) no runner is guessed.
  function states(lanes, warmSlots) {
    const kept = new Set(warmSlots || []);
    return new Map(lanes.map(lane => [lane, lane.state === "allocated" && kept.has(lane.id) ? "warm" : lane.state]));
  }

  function group(kind, lanes, stateOf, owner, now, extra) {
    const meta = KINDS[kind];
    const rows = lanes.map(lane => runnerRow(lane, stateOf.get(lane), owner, now));
    return el("div", { class: "runner-group", role: "rowgroup", "aria-label": meta.title },
      el("div", { class: "runner-group-title" }, el("b", {}, meta.title), el("span", { class: "muted" }, meta.note)),
      ...(rows.length ? rows : [el("p", { class: "muted runner-none" }, "None right now.")]), extra || null);
  }

  // The rest of the waiting-job pool: free while the listener answers, unknown while it does not
  // (the host reports no state for those slots then, as for the runner slots).
  function waitRest(count, listenerState) {
    if (listenerState === "up") return freeRow(count, "Not counted in the runner slots above.");
    return el("div", { class: "runner-row state-unknown", role: "row" }, el("span", { role: "cell" }, `${count} unknown`),
      el("span", { class: "muted runner-free-note", role: "cell" }, "The runner listener is not answering, so these slots' state is not known."));
  }

  function freeRow(count, text) {
    return el("div", { class: "runner-row state-free", role: "row" }, el("span", { role: "cell" }, `${count} free`),
      el("span", { class: "muted runner-free-note", role: "cell" }, text));
  }

  // One section per kind of slot. Build-and-test and AI QA share the runner slots; waiting jobs have
  // a pool of their own. Free slots are not runners yet, so each pool shows them as one count.
  function runnerTable(capacity, owner, now) {
    const used = capacity.lanes.filter(lane => !isFree(lane));
    const byKind = kind => used.filter(lane => kindOf(lane) === kind);
    const stateOf = states(used, capacity.history?.warm_slots);
    const limits = capacity.limits || {};
    const sharedFree = capacity.lanes.filter(isFree).length;
    // With the listener down a slot's state is unknown and names no kind; say so rather than drop it.
    const unknown = capacity.lanes.filter(lane => !isFree(lane) && !KINDS[kindOf(lane)]).length;
    const sharedLimit = limits.slots ?? (byKind("ci").length + byKind("qae").length + sharedFree);
    const header = el("div", { class: "runner-row runner-head muted", role: "row" },
      ...["Runner", "State", "Job", "Repository", "Time"].map(name => el("span", { role: "columnheader" }, name)));
    const shared = el("div", { class: "runner-pool" },
      el("h3", {}, `Runner slots: ${byKind("ci").length + byKind("qae").length} of ${sharedLimit} in use`),
      group("ci", byKind("ci"), stateOf, owner, now),
      group("qae", byKind("qae"), stateOf, owner, now, limits.qae_concurrency ?
        el("p", { class: "muted runner-none" }, `At most ${limits.qae_concurrency} at once.`) : null),
      sharedFree ? freeRow(sharedFree, "A runner starts in a free slot when a job arrives.") : null,
      unknown ? el("div", { class: "runner-row state-unknown", role: "row" }, el("span", { role: "cell" }, `${unknown} unknown`),
        el("span", { class: "muted runner-free-note", role: "cell" }, "The runner listener is not answering, so these slots' state is not known.")) : null);
    const waiting = byKind("wait");
    const pools = [shared];
    if (limits.wait_slots || waiting.length) {
      const waitLimit = limits.wait_slots ?? waiting.length;
      pools.push(el("div", { class: "runner-pool" }, el("h3", {}, `Waiting-job slots: ${waiting.length} of ${waitLimit} in use`),
        group("wait", waiting, stateOf, owner, now),
        waitLimit > waiting.length ? waitRest(waitLimit - waiting.length, capacity.listener_state) : null));
    }
    return el("div", { class: "runner-table", role: "table", "aria-label": "Runners now" }, header, ...pools);
  }

  function render(content, snapshot, { heading, empty, sourceBanner, rerender }) {
    const capacity = snapshot.telemetry.available ? snapshot.telemetry.capacity : null;
    content.replaceChildren(heading("Capacity", "What each runner is doing, and how busy the lane has been."));
    if (!capacity || !capacity.available) { content.append(empty("Capacity telemetry unavailable", "No valid current-owner capacity snapshot was provided.")); return; }
    if (capacity.stale) content.append(sourceBanner(`Capacity telemetry is stale. Last sample: ${formatTime(capacity.sampled_at)}.`, "warning"));
    const now = Date.now();
    const ranges = capacity.history?.ranges || [];
    const range = ranges.find(row => row.name === chosenRange) || null;
    const limit = capacity.history?.limit || capacity.limits?.slots;
    const lanes = el("section", { class: "panel" }, el("div", { class: "panel-title" }, el("h2", {}, `${snapshot.owner} runners`),
      el("span", { class: "muted" }, `Sampled ${formatTime(capacity.sampled_at)}`)), limitsLine(capacity.limits),
      summary(capacity, range), range && limit ? chart(range, limit, rerender) :
        el("p", { class: "muted" }, "No history yet: the collector records it from its next samples."),
      runnerTable(capacity, snapshot.owner, now));
    content.append(lanes, hostPanel(capacity));
  }

  const api = { render, slotName, waitText, limitsLine };
  if (typeof module === "object" && module.exports) module.exports = api;
  else window.VVCapacity = api;
})();
