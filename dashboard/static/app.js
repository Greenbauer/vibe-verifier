(function () {
  "use strict";
  const { BOT_META, element: el, link, duration, since, formatTime, bytes, badge,
    diskUsage, flattenPulls, filterPulls, stepTotals, combinedCategory, currentWork } = VV;
  const content = document.querySelector("#content");
  const announcement = document.querySelector("#announcement");
  const state = { ...VV.restoreViewState(null), ...VV.parseRoute(location.hash) };
  let snapshot = null;
  let loading = false;

  function restoreView(owner) {
    try { Object.assign(state, VV.restoreViewState(sessionStorage.getItem(`vv-dashboard-view:${owner}`))); }
    catch (_) { /* Storage can be disabled by the browser. */ }
  }

  function rememberView() {
    if (!snapshot) return;
    const { view, selected, ...preferences } = state;
    try { sessionStorage.setItem(`vv-dashboard-view:${snapshot.owner}`, JSON.stringify(preferences)); }
    catch (_) { /* Navigation still works when storage is unavailable. */ }
  }

  // Every view change sets the URL hash, which adds a history entry; hashchange then draws it.
  // Back and forward fire the same event, so they draw through the same path.
  function go(view, selected = null) {
    const hash = VV.routeHash({ view, selected });
    if (location.hash !== hash) location.hash = hash;
    else show();
  }

  // An empty or unknown hash shows pull requests; rewrite it in place so the address names that view.
  function replaceUnknownHash() {
    const hash = VV.routeHash(state);
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }

  function show() {
    Object.assign(state, VV.parseRoute(location.hash));
    replaceUnknownHash();
    render(); content.focus({ preventScroll: true }); window.scrollTo(0, 0);
  }

  function announce(message) { announcement.textContent = message; }
  function heading(title, subtitle) {
    return el("div", { class: "section-heading" }, el("div", {}, el("h1", {}, title), el("p", { class: "muted" }, subtitle)));
  }
  function empty(title, detail) {
    return el("section", { class: "panel empty" }, el("h2", {}, title), el("p", { class: "muted" }, detail));
  }
  function sourceBanner(text, tone) {
    return el("div", { class: `source-banner ${tone || ""}`, role: "status" }, text);
  }

  function renderChrome() {
    document.querySelector("#owner").textContent = snapshot.owner;
    const sampled = snapshot.github.sampled_at;
    document.querySelector("#source-stamp").textContent = sampled ? `GitHub sampled ${formatTime(sampled)}${snapshot.github.refreshing ? " · refreshing" : ""}` : snapshot.github.refreshing ? "Fetching GitHub data…" : "GitHub unavailable";
    document.querySelectorAll(".sidebar button").forEach(button => {
      if (button.dataset.view === state.view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const agents = snapshot.agents?.rows || [];
    if (agents.length && !agents.some(agent => agent.id === state.failureBot)) state.failureBot = agents[0].id;
    renderBots();
  }

  function recentRunLabel(value, recent) {
    if (recent[0]?.category === "failed") return "Latest run failed";
    if (recent.length) return "Last 2h";
    return value.coverage?.history === "complete" ? "No runs in 2h" : "History incomplete";
  }

  function renderBots() {
    const strip = document.querySelector("#bot-strip");
    strip.replaceChildren(el("span", { class: "bot-strip-title" }, "BOTS", el("small", {}, "Last 2h · newest first")));
    const agents = snapshot.agents?.rows || [];
    if (!agents.length) strip.append(el("span", { class: "muted" }, "Agent roster not configured"));
    for (const value of agents) {
      const role = value.id;
      const meta = { name: value.name, ...BOT_META[value.role] };
      const recent = value.recent_2h || [];
      const button = el("button", {
        class: `bot-pill${recent[0]?.category === "failed" ? " has-failure" : ""}`,
        title: value.source || "Agent data unavailable",
        "aria-label": `${meta.name}: ${value.state || "unknown"}. Open usage and recent outcomes`,
        onclick: () => { state.failureBot = role; go("usage"); }
      });
      button.style.setProperty("--bot", meta.color);
      const dots = el("span", { class: "run-dots", "aria-label": recent.length ? "Newest first completed outcomes" : "No completed outcomes in two hours" });
      recent.forEach((run, index) => dots.append(el("i", { class: `dot dot-${run.category}${index === 0 ? " newest" : ""}`, title: `${index === 0 ? "Latest: " : ""}${run.category}: ${formatTime(run.completed_at)}` })));
      button.append(el("span", { class: "bot-name" }, meta.name), badge(value.state || "unknown"), dots,
        el("span", { class: recent[0]?.category === "failed" ? "failure-note" : "quiet-note" }, recentRunLabel(value, recent)));
      strip.append(button);
    }
  }

  function coverage() {
    const value = snapshot.github.coverage;
    const errors = snapshot.github.errors || [];
    const text = `${value.label}: ${value.selected}. ${value.readable} read successfully in this sample.`;
    return sourceBanner(text + (errors.length ? " Some GitHub data is unavailable or stale." : ""), errors.length ? "warning" : "");
  }

  function prFilters(pulls) {
    const repositories = [...new Set(pulls.map(pull => pull.repository))].sort();
    const search = el("input", { type: "search", id: "pr-search", placeholder: "Search PRs", value: state.query,
      "aria-label": "Search pull requests", oninput: event => { state.query = event.target.value; renderPrRows(); } });
    const select = el("select", { id: "repo-filter", "aria-label": "Repository filter", onchange: event => {
      state.repository = event.target.value; renderPrRows();
    } }, el("option", { value: "all" }, "All selected repositories"));
    repositories.forEach(repository => select.append(el("option", { value: repository, selected: state.repository === repository }, repository.split("/")[1])));
    const check = (label, key) => el("label", { class: "check-filter" },
      el("input", { type: "checkbox", checked: state[key], onchange: event => { state[key] = event.target.checked; renderPrRows(); } }), label);
    return el("div", { class: "toolbar" }, search, select, check("Subscribed only", "subscribed"), check("Needs attention", "attention"));
  }

  function progress(pull) {
    const totals = stepTotals(pull);
    if (!totals.known) return el("div", { class: "progress-copy" }, el("b", {}, "Step total unavailable"), el("span", {}, "Progress is not shown as complete"));
    const meter = el("div", { class: "step-meter", role: "progressbar", "aria-valuemin": 0,
      "aria-valuemax": totals.total, "aria-valuenow": totals.completed });
    meter.append(el("span", { style: `width:${totals.total ? totals.completed / totals.total * 100 : 0}%` }));
    return el("div", { class: "progress-copy" }, el("b", {}, `${totals.completed}/${totals.total} steps`),
      meter, el("span", {}, `${totals.remaining} remaining`));
  }

  function renderPrRows() {
    rememberView();
    const all = flattenPulls(snapshot);
    const pulls = filterPulls(all, state);
    const rows = document.querySelector("#pr-rows");
    const count = document.querySelector("#pr-count");
    if (!rows || !count) return;
    count.textContent = `${pulls.length} pull request${pulls.length === 1 ? "" : "s"}`;
    rows.replaceChildren();
    if (!pulls.length) {
      rows.append(snapshot.github.refreshing && !snapshot.github.sampled_at ? empty("Loading pull requests", "Reading selected repositories from GitHub.") : empty("No matching pull requests", "Change a filter or wait for the next successful GitHub sample."));
      return;
    }
    pulls.forEach(pull => {
      const category = combinedCategory(pull);
      const work = currentWork(pull);
      const button = el("button", { class: "pr-row", onclick: () => go("prs", `${pull.repository}#${pull.number}`),
      "aria-label": `Open ${pull.repository} pull request ${pull.number}: ${pull.title}` },
      el("span", { class: "pr-identity" }, el("b", {}, pull.title),
        el("small", {}, `${pull.repository.split("/")[1]} #${pull.number} · ${pull.author || "unknown"} · ${pull.head_sha ? pull.head_sha.slice(0, 8) : "head changing"}`)),
      el("span", { class: "pr-work" }, badge(category), el("small", {}, pull.attention_reason)),
      progress(pull),
      el("span", { class: "pr-age" }, el("b", {}, since(pull.created_at, Date.now())), el("small", {}, work ? `${work.name} · ${duration(work.elapsed)}` : "PR age")),
      el("span", { class: "chevron", "aria-hidden": "true" }, "›"));
      if (pull.stale) button.classList.add("stale-row");
      rows.append(button);
    });
  }

  function renderPulls() {
    state.selected = null;
    const pulls = flattenPulls(snapshot);
    content.replaceChildren(heading("Pull requests", "Open pull requests and current-head evidence from selected repositories."),
      coverage(), prFilters(pulls), el("div", { class: "list-heading" }, el("span", { id: "pr-count" }),
        el("span", {}, "Current work"), el("span", {}, "Steps"), el("span", {}, "Age")),
      el("div", { id: "pr-rows", class: "pr-list" }));
    renderPrRows();
  }

  function checkRow(row) {
    return el("div", { class: "check-row" }, el("span", {}, el("b", {}, row.name), el("small", {}, row.provider)),
      badge(row.category), el("span", { class: "mono" }, duration(row.elapsed_seconds)),
      link("Original evidence", row.details_url, snapshot.owner));
  }

  function timeline(pull) {
    const jobs = (pull.runs || []).flatMap(run => (run.jobs || []).map(job => ({ ...job, run })));
    if (!jobs.length) return empty("Actions timing unavailable", "Third-party checks remain listed above. No joined Actions jobs were returned.");
    const starts = jobs.map(job => Date.parse(job.started_at || job.created_at)).filter(Number.isFinite);
    const ends = jobs.map(job => Date.parse(job.completed_at) || Date.now()).filter(Number.isFinite);
    const start = Math.min(...starts), end = Math.max(...ends), span = Math.max(1, end - start);
    const panel = el("section", { class: "panel" }, el("h2", {}, "Parallel job timeline"),
      el("p", { class: "muted" }, "Bars share wall-clock time. Job durations are not summed."));
    jobs.forEach(job => {
      const began = Date.parse(job.started_at || job.created_at), finished = Date.parse(job.completed_at) || Date.now();
      const bar = el("span", { class: `timeline-bar status-${job.category}` });
      bar.style.left = `${Math.max(0, (began - start) / span * 100)}%`;
      bar.style.width = `${Math.max(1, (finished - began) / span * 100)}%`;
      const details = el("details", { class: "job-detail", "data-job-id": job.id },
        el("summary", {}, el("span", {}, job.name), badge(job.category), el("span", { class: "mono" }, duration(job.elapsed_seconds))),
        el("div", { class: "timeline-track" }, bar),
        el("p", { class: "muted" }, `Created-to-start: ${duration(job.queue_seconds)} · run attempt ${job.run.attempt}`));
      (job.steps || []).forEach(step => details.append(el("div", { class: "step-row" }, badge(step.category),
        el("span", {}, step.name), el("span", { class: "mono" }, duration(step.elapsed_seconds)))));
      const evidence = link("Open job on GitHub", job.html_url, snapshot.owner);
      if (evidence) details.append(evidence);
      panel.append(details);
    });
    return panel;
  }

  function renderDetail() {
    const pull = flattenPulls(snapshot).find(item => `${item.repository}#${item.number}` === state.selected);
    const back = el("button", { class: "back", onclick: () => go("prs") }, "‹ Pull requests");
    if (!pull) {
      content.replaceChildren(back, empty(snapshot.github.refreshing ? "Loading pull request" : "Pull request unavailable",
        "The selected pull request is not in the current sample. It will appear when available, or you can return to the list."));
      return;
    }
    const header = el("div", { class: "detail-heading" }, el("div", {}, el("h1", {}, pull.title),
      el("p", { class: "muted" }, `${pull.repository} #${pull.number} · current head ${pull.head_sha ? pull.head_sha.slice(0, 12) : "changed"}`)),
      link("Open pull request on GitHub", pull.html_url, snapshot.owner, "primary-link"));
    const evidence = [...(pull.checks || []), ...(pull.statuses || [])];
    const checks = el("section", { class: "panel" }, el("h2", {}, "Current-head checks"), progress(pull));
    evidence.forEach(row => checks.append(checkRow(row)));
    if (!evidence.length) checks.append(el("p", { class: "muted" }, "No current-head check evidence is available."));
    content.replaceChildren(back, header, sourceBanner(pull.attention_reason, pull.attention ? "warning" : ""), checks, timeline(pull));
  }

  function quota(account) {
    const panel = el("div", { class: "quota-row" });
    if (!account.quota_windows.length) return panel.appendChild(el("span", { class: "muted" }, "Quota window unavailable"));
    account.quota_windows.forEach(window => panel.append(el("div", { class: "quota" },
      el("b", {}, window.name), el("span", {}, `${window.used_percent}% used`),
      el("small", {}, `Resets ${formatTime(window.resets_at)}`),
      window.allowance_tokens === null ? null :
        el("small", {}, `${window.allowance_tokens.toLocaleString()} token allowance reported`))));
    return panel;
  }

  function failurePanel() {
    const agents = snapshot.agents?.rows || [];
    const roles = Object.fromEntries(agents.map(agent => [agent.id, agent]));
    const panel = el("section", { class: "panel failures" }, el("h2", {}, "Recent bot failures"),
      el("p", { class: "muted" }, "Last five completed runs per bot in seven days. Select a bot for detail."));
    const buttons = el("div", { class: "failure-buttons" });
    agents.forEach(agent => {
      const role = agent.id;
      const runs = roles[role]?.recent_7d || [];
      const button = el("button", { "aria-pressed": String(state.failureBot === role), onclick: () => { state.failureBot = role; render(); } },
        el("span", { style: `color:${BOT_META[agent.role].color}` }, agent.name));
      const dots = el("span", { class: "run-dots" });
      runs.forEach(run => dots.append(el("i", { class: `dot dot-${run.category}` })));
      button.append(dots); buttons.append(button);
    });
    panel.append(buttons);
    const selectedRole = roles[state.failureBot];
    const failure = (selectedRole?.recent_7d || []).find(run => run.category === "failed");
    panel.append(!selectedRole || (!failure && selectedRole.coverage?.history !== "complete") ? el("p", { class: "muted" }, "Bot history is unavailable.") :
      failure ? el("div", { class: "failure-detail" }, badge("failed"), el("b", {}, failure.name),
      el("span", {}, `${failure.repository ? failure.repository + " · " : ""}${formatTime(failure.completed_at)} · ${duration(failure.elapsed_seconds)}`),
      link("Open original job", failure.html_url, snapshot.owner)) : el("p", { class: "muted" }, "No failed completed run in the available seven-day history."));
    return panel;
  }

  function usageCharts(usage) {
    const samples = usage.samples || [], now = Date.now();
    // Hours after the source's last observation are unobserved, not zero, when the source goes stale.
    const through = Math.min(...[usage.sampled_at, usage.history_sampled_at].map(Date.parse).filter(Number.isFinite));
    const sampleAccounts = new Set(samples.map(sample => sample.account));
    const account = sampleAccounts.size === 1 ? usage.accounts.find(item => sampleAccounts.has(item.id)) : null;
    const pace = VVCharts.pacePerHour(account?.quota_windows.find(item => item.allowance_tokens !== null));
    const bots = (snapshot.agents?.rows || []).map(agent => ({ name: agent.name, color: BOT_META[agent.role].color,
      burn: VVCharts.hourlyBurn(samples.filter(sample => sample.bot === agent.id), now, through) }));
    const observed = bots.map(bot => bot.burn).filter(Boolean);
    const all = { name: "All bots", color: "rgba(255,255,255,0.6)", all: true, burn: observed.length ? VVCharts.sumBurns(observed) : null };
    const shared = VVCharts.ceiling(observed);
    return el("section", { class: "panel usage-charts" }, el("h2", {}, "Token burn pattern"),
      el("p", { class: "muted" }, "Tokens each bot used, hour by hour. Solid: last 24 hours. Dashed: a usual day. Flat: the pace that lasts until the plan resets."),
      el("div", { class: "burn-cards" }, bots.map(bot => burnCard(bot, shared, now, null)),
        burnCard(all, VVCharts.ceiling(all.burn ? [all.burn] : [], pace), now, pace)),
      el("p", { class: "muted pace-note" }, pace === null ?
        "No flat pace line: it needs a reported token allowance for the same account. Subscription percentages alone cannot provide it." :
        "Flat line on All bots: remaining token allowance divided by time until reset."));
  }

  // One card: name and last-24h peak, the hour-of-day chart (or "Not yet observed"), and its totals.
  function burnCard(row, maximum, now, pace) {
    const burn = row.burn, days = burn?.baselineDays ?? null;
    const card = el("article", { class: `burn-card${row.all ? " all-bots" : ""}`, style: `--accent:${row.color}` });
    const peak = Math.max(...(burn?.last24h || []).filter(Number.isFinite));
    card.append(el("div", { class: "burn-head" }, el("b", {}, row.name),
      el("span", { title: "Most tokens in one clock hour of the last 24 hours" }, Number.isFinite(peak) ? `peak ${VVCharts.short(peak)}/h` : null)));
    if (!burn) {
      card.append(el("p", { class: "burn-empty muted" }, "Not yet observed"));
      return card;
    }
    const usual = `prior ${days}-day hourly average`;
    const method = days === null ? "Tokens in the last 24 hours. A usual day appears once every hour has an observed prior day." :
      `Tokens in the last 24 hours, and a usual day averaged over the ${days} prior day${days === 1 ? "" : "s"} observed so far${days < 6 ? " (not a full week yet)" : ""}.`;
    card.append(VVCharts.drawBurn(burn, { color: row.color, maximum, nowMs: now, pace, usualTitle: `${row.name}: ${usual}`,
      label: `${row.name}: tokens per clock hour, ${days === null ? "no usual day yet" : `against the ${usual}`}`,
      paceTitle: `Pace to reset: ${Math.round(pace).toLocaleString()} tokens per hour spends the remaining allowance exactly at its reset.` }),
    el("div", { class: "burn-totals", title: method }, VVCharts.totals(burn)));
    return card;
  }

  function renderUsage() {
    const usage = snapshot.agents?.usage;
    content.replaceChildren(heading("Bot usage", "Measured tokens and subscription capacity."));
    if (usage?.available) {
      if (usage.stale) content.append(sourceBanner(`Usage telemetry is stale. Last sample: ${formatTime(usage.sampled_at)}.`, "warning"));
      const quotas = usage.accounts.filter(account => account.quota_windows.length);
      if (quotas.length) {
        const panel = el("section", { class: "panel subscription-panel" });
        quotas.forEach(account => panel.append(el("div", { class: "account-heading" },
          el("h2", {}, account.label), quota(account))));
        content.append(panel);
      }
      content.append(usageCharts(usage), el("p", { class: "muted coverage-note" }, usage.completeness));
    } else content.append(usageCharts({ samples: [], accounts: [] }), sourceBanner("Usage source is unavailable.", "warning"));
    content.append(failurePanel());
  }

  function metric(label, value, percent) {
    const row = el("div", { class: "host-metric" }, el("span", {}, label), el("b", {}, value));
    if (Number.isFinite(percent)) row.append(el("div", { class: "metric-meter", role: "meter", "aria-valuenow": percent,
      "aria-valuemin": 0, "aria-valuemax": 100 }, el("span", { style: `width:${percent}%` })));
    return row;
  }

  function renderCapacity() {
    const capacity = snapshot.telemetry.available ? snapshot.telemetry.capacity : null;
    content.replaceChildren(heading("Capacity", "Your runner capacity and the shared machine’s resources."));
    if (!capacity || !capacity.available) { content.append(empty("Capacity telemetry unavailable", "No valid current-owner capacity snapshot was provided.")); return; }
    if (capacity.stale) content.append(sourceBanner(`Capacity telemetry is stale. Last sample: ${formatTime(capacity.sampled_at)}.`, "warning"));
    const host = capacity.host || {};
    const memoryPercent = Number.isFinite(host.memory_used_bytes) && host.memory_total_bytes ? host.memory_used_bytes / host.memory_total_bytes * 100 : null;
    const disk = diskUsage(host);
    content.append(el("section", { class: "panel host-panel" }, el("div", { class: "panel-title" }, el("h2", {}, "SHARED HOST"),
      el("span", { class: "muted" }, `Sampled ${formatTime(capacity.sampled_at)}`)), el("div", { class: "host-grid" },
      metric("CPU sampled", Number.isFinite(host.cpu_percent) ? `${host.cpu_percent}%` : "Unavailable", host.cpu_percent),
      metric("Memory", Number.isFinite(host.memory_used_bytes) ? `${bytes(host.memory_used_bytes)} / ${bytes(host.memory_total_bytes)}` : "Unavailable", memoryPercent),
      metric("Workspace disk", disk ? `${bytes(disk.used)} / ${bytes(disk.total)}` : "Unavailable", disk?.percent))));
    const lanes = el("section", { class: "panel" }, el("div", { class: "panel-title" }, el("h2", {}, `${snapshot.owner} runners (${capacity.lanes.length})`),
      el("span", { class: "muted" }, "Registered and on-demand capacity")), el("div", { class: "lane-grid" }));
    const grid = lanes.querySelector(".lane-grid");
    capacity.lanes.forEach(lane => {
      const card = el("article", { class: `lane status-${lane.state}` }, el("div", { class: "lane-heading" }, el("b", { class: "mono" }, lane.id), badge(lane.state)),
        el("p", { class: "muted" }, lane.registered === true ? "Runner registered" : lane.registered === false ? "No runner registered" : "Registration unknown"));
      if (lane.job) card.append(el("div", {}, el("p", {}, lane.job.name), el("p", { class: "muted" }, lane.job.repository), link("Open job", lane.job.url, snapshot.owner)));
      else card.append(el("p", { class: "muted" }, lane.state === "provisionable" ? "Provisionable when work arrives" :
        lane.state === "busy" ? "Busy; job details not yet matched" : "No current same-owner job"));
      if (lane.labels.length) card.append(el("small", { class: "muted" }, lane.labels.join(" · ")));
      grid.append(card);
    });
    const limits = capacity.limits;
    if (limits) lanes.prepend(el("p", { class: "muted lane-limits" },
      `${limits.slots} shared CI/QAE slots · QAE maximum ${limits.qae_concurrency} · CPU cap ${limits.cpu_quota_cores ?? "unknown"} cores · Memory cap ${bytes(limits.memory_max_bytes)}`));
    content.append(lanes);
  }

  function render() {
    if (!snapshot) return;
    renderChrome();
    if (state.selected) renderDetail();
    else if (state.view === "usage") renderUsage();
    else if (state.view === "capacity") renderCapacity();
    else renderPulls();
    rememberView();
    const title = state.selected || { prs: "Pull requests", usage: "Bot usage", capacity: "Capacity" }[state.view];
    document.title = `${title} · Vibe Verifier`;
    announce(`Showing ${state.selected ? "pull request detail" : state.view}`);
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch("/api/dashboard", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("source unavailable");
      const next = await response.json();
      if (!snapshot) restoreView(next.owner);
      snapshot = next;
      const openedJobs = [...content.querySelectorAll("details[open]")].map(item => item.dataset.jobId);
      const focused = document.activeElement;
      const filterId = ["pr-search", "repo-filter"].includes(focused?.id) ? focused.id : null;
      const selection = filterId === "pr-search" ? [focused.selectionStart, focused.selectionEnd] : null;
      render();
      content.querySelectorAll("details[data-job-id]").forEach(item => { item.open = openedJobs.includes(item.dataset.jobId); });
      const replacement = filterId && document.getElementById(filterId);
      if (replacement) {
        replacement.focus({ preventScroll: true });
        if (selection) replacement.setSelectionRange(...selection);
      }
    } catch (_) {
      content.replaceChildren(empty("Dashboard unavailable", "The local read-only source could not be loaded."));
      document.querySelector("#bot-strip").replaceChildren(el("span", { class: "muted" }, "Bot status unavailable"));
    } finally { loading = false; }
  }

  document.querySelectorAll(".sidebar button").forEach(button => button.addEventListener("click", () => go(button.dataset.view)));
  replaceUnknownHash();
  window.addEventListener("hashchange", show);
  window.addEventListener("pagehide", rememberView);
  load();
  window.setInterval(load, 30000);
})();
