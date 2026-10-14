(function () {
  "use strict";
  const { BOT_META, element: el, link, safeUrl, duration, ageLabel, since, ageClass, secondsClass, ciTiming, formatTime, bytes, badge, botState, sourceStamp,
    diskUsage, flattenPulls, filterPulls, groupPulls, repositoriesWithPulls, coverageWarning, checkTotals, combinedCategory, meterSegments, meterLabel, runningWork, unresolvedMark, behindMark,
    quotaWindowLabel, quotaDisplayPercent, quotaPace, quotaPacePhrase, paceHeader, quotaDeltaLabel, quotaTone, quotaCountdown, quotaGroups } = VV;
  const content = document.querySelector("#content");
  const announcement = document.querySelector("#announcement");
  const state = { ...VV.restoreViewState(null), ...VV.parseRoute(location.hash) };
  let snapshot = null;
  let loading = false, retry = 0;
  let refreshFailed = false;

  // localStorage, not sessionStorage: a reload, a new tab or a reopened window all keep the filters.
  function restoreView(owner) {
    try { Object.assign(state, VV.restoreViewState(localStorage.getItem(`vv-dashboard-view:${owner}`))); }
    catch (_) { /* Storage can be disabled by the browser. */ }
  }

  function rememberView() {
    if (!snapshot) return;
    const { view, ...preferences } = state;
    try { localStorage.setItem(`vv-dashboard-view:${snapshot.owner}`, JSON.stringify(preferences)); }
    catch (_) { /* Navigation still works when storage is unavailable. */ }
  }

  // Every view change sets the URL hash, which adds a history entry; hashchange then draws it.
  // Back and forward fire the same event, so they draw through the same path.
  function go(view) {
    const hash = VV.routeHash({ view });
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
    render(); content.focus({ preventScroll: true }); window.scrollTo(0, 0); content.scrollTo?.(0, 0);
  }

  function announce(message) { announcement.textContent = message; }
  function heading(title, subtitle, warning) {
    const mark = warning && el("span", { class: "title-warning", role: "status" }, el("i", { class: "warning-dot", "aria-hidden": "true" }), warning);
    return el("div", { class: "section-heading" }, el("div", {}, el("h1", {}, title, mark), el("p", { class: "muted" }, subtitle)));
  }
  function empty(title, detail) {
    return el("section", { class: "panel empty" }, el("h2", {}, title), el("p", { class: "muted" }, detail));
  }
  function sourceBanner(text, tone) {
    return el("div", { class: `source-banner ${tone || ""}`, role: "status" }, text);
  }

  function renderChrome() {
    document.querySelector("#owner").textContent = snapshot.owner;
    document.querySelector("#source-stamp").textContent = sourceStamp(snapshot.github);
    document.querySelectorAll(".sidebar button").forEach(button => {
      button.hidden = button.dataset.view === "work" && !snapshot.work?.configured;
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
      const recent = value.recent_2h || [], shown = botState(value);
      const button = el("button", {
        class: `bot-pill${recent[0]?.category === "failed" ? " has-failure" : ""}`,
        title: value.source || "Agent data unavailable",
        "aria-label": `${meta.name}: ${shown.label}. Open usage and recent outcomes`,
        onclick: () => { state.failureBot = role; go("usage"); }
      });
      button.style.setProperty("--bot", meta.color);
      const dots = el("span", { class: "run-dots", "aria-label": recent.length ? "Newest first completed outcomes" : "No completed outcomes in two hours" });
      recent.forEach((run, index) => dots.append(el("i", { class: `dot dot-${run.category}${index === 0 ? " newest" : ""}`, title: `${index === 0 ? "Latest: " : ""}${run.category}: ${formatTime(run.completed_at)}` })));
      button.append(el("span", { class: "bot-name" }, meta.name), badge(shown.status, shown.label), dots,
        el("span", { class: recent[0]?.category === "failed" ? "failure-note" : "quiet-note" }, recentRunLabel(value, recent)));
      strip.append(button);
    }
  }

  function prFilters(pulls) {
    const repositories = repositoriesWithPulls(pulls);
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

  function messageIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "comment-icon");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", "M1 2.75C1 1.784 1.784 1 2.75 1h10.5c.966 0 1.75.784 1.75 1.75v7.5A1.75 1.75 0 0 1 13.25 12H9.06l-2.573 2.573A1.458 1.458 0 0 1 4 13.543V12H2.75A1.75 1.75 0 0 1 1 10.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h2a.75.75 0 0 1 .75.75v2.19l2.72-2.72a.75.75 0 0 1 .53-.22h4.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z");
    svg.append(path);
    return svg;
  }

  function commentMark(pull) {
    const mark = unresolvedMark(pull);
    if (!mark) return null;
    return el("span", {
      class: "comment-mark", title: mark.title, "aria-label": mark.title
    }, messageIcon(), mark.shown);
  }

  // CI time on the head, then what the last push was. Checks still from the previous push
  // say nothing about this one's CI, so that row shows how long ago it was pushed instead.
  function pushLine(pull, now) {
    const push = pull.push;
    const known = push && typeof push.pushed_at === "string" && typeof push.kind === "string" && push.kind;
    const ci = pull.head_changed ? null : ciTiming(pull, now);
    const pushed = known ? `pushed ${since(push.pushed_at, now)} ago` : "Last push unavailable";
    if (!ci && !known) return el("span", { class: "push-line", title: pushed }, pushed);
    const when = !ci ? pushed : ci.seconds == null ? ci.label : `${ci.label} ${ageLabel(ci.seconds)}`;
    const tone = ci ? secondsClass(ci.seconds, "ci") : ageClass(push.pushed_at, now, "push");
    return el("span", { class: "push-line", title: [ci ? when : null, pushed, known ? push.kind : null].filter(Boolean).join(" · ") },
      el("span", { class: tone }, when), known ? el("span", { class: "push-kind" }, `· ${push.kind}`) : null);
  }

  function progress(pull, now = Date.now()) {
    const totals = checkTotals(pull);
    const mark = commentMark(pull);
    const segments = totals.known ? meterSegments(totals) : [];
    const meter = !totals.known ? el("b", { class: "progress-lead" }, "No checks reported")
      : el("div", { class: "check-meter", role: "img", "aria-label": meterLabel(totals) });
    if (totals.known) segments.forEach(segment => meter.append(el("span", {
      class: `seg-${segment.state}`, style: `flex:${segment.count} 1 0`, title: `${segment.count} ${segment.label}`,
      "aria-hidden": "true"
    })));
    const open = segments.filter(segment => segment.state !== "success")
      .map(segment => `${segment.count} ${segment.label.toLowerCase()}`);
    const caption = totals.known ? (open.join(" · ") || "All passed") : "Progress is not shown as complete";
    return el("div", { class: "progress-copy" },
      el("div", { class: mark ? "progress-layout has-mark" : "progress-layout" },
        meter, mark,
        el("div", { class: "progress-caption" },
          el("span", { class: "progress-counts" }, caption),
          pushLine(pull, now))));
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
    groupPulls(pulls).forEach(group => {
      const list = el("div", { class: "pr-list" });
      group.pulls.forEach(pull => list.append(prRow(pull)));
      const name = group.repository.split("/")[1];
      rows.append(el("section", { class: "pr-group", "aria-label": `${name} pull requests` },
        el("h2", { class: "pr-group-title" }, name, el("small", {},
          `${group.pulls.length} open · ${Number.isFinite(group.activity) ? `last activity ${duration(Math.max(0, (Date.now() - group.activity) / 1000))} ago` : "last activity unavailable"}`)),
        list));
    });
  }

  function runningNames(pull) {
    const jobs = new Map((pull.runs || []).flatMap(run => (run.jobs || []).map(job => [job.id, job])));
    const at = row => {
      const step = ((jobs.get(row.id) || {}).steps || []).find(item => item.status === "in_progress");
      const when = Date.parse((step && step.started_at) || row.started_at);
      return Number.isFinite(when) ? when : -Infinity;
    };
    const checks = (pull.checks || []).filter(row => row.status === "in_progress").sort((a, b) => at(b) - at(a));
    return runningWork({ ...pull, checks }).map(item => item.name).filter(Boolean);
  }

  function workLines(names, reason) {
    const extra = Math.max(0, names.length - 2);
    return [...names.slice(0, 2).map(line => el("small", { class: "work-line", title: line }, line)),
      extra ? el("small", { class: "work-more" }, `+${extra} more`) : null,
      reason ? el("span", { class: "sr-only" }, reason) : null];
  }
  function workDetail(pull) {
    const reason = pull.attention_reason || "";
    const running = runningNames(pull);
    if (running.length) return workLines(running, reason);
    const rows = [...(pull.checks || []), ...(pull.statuses || []), ...(pull.expected || [])];
    const named = list => list.map(row => row && typeof row.name === "string" ? row.name : "").filter(Boolean);
    const failed = named(rows.filter(row => row.category === "failed"));
    if (failed.length) return workLines(failed, reason);
    const required = named(pull.expected || []);
    if (required.length) return workLines(required, reason);
    const queued = rows.filter(row => row.status === "queued").length;
    if (queued) return workLines([`${queued} ${queued === 1 ? "check" : "checks"} queued`], reason);
    return [el("small", {}, reason)];
  }

  function behindBadge(pull) {
    const mark = behindMark(pull);
    return mark ? el("span", { class: "behind-mark", title: mark.title, "aria-label": mark.title }, mark.shown) : null;
  }

  function prRow(pull) {
    const category = combinedCategory(pull);
    const href = safeUrl(pull.html_url, snapshot.owner);
    const now = Date.now();
    const opened = href ? `Open ${pull.repository} pull request ${pull.number} on GitHub: ${pull.title}` : null;
    const reason = pull.attention_reason || "", ready = pull.merge_ready && !pull.stale;
    return el(href ? "a" : "div", {
      class: "pr-row",
      href, target: href ? "_blank" : null, rel: href ? "noreferrer" : null,
      "aria-label": opened ? opened + (ready ? ". Fully merge-ready" : "") : null
    },
    el("span", { class: "pr-identity" },
      el("b", { class: ready ? "merge-ready" : null, title: ready ? "Fully merge-ready" : null }, pull.title),
      el("small", {}, `#${pull.number} · ${pull.author || "unknown"} · ${pull.head_sha ? pull.head_sha.slice(0, 8) : "head changing"}`,
        behindBadge(pull))),
    el("span", { class: "pr-work", title: reason || null }, badge(category), ...workDetail(pull)),
    progress(pull, now),
    el("span", { class: "pr-age" },
      el("span", { class: "age-stat" }, el("b", { class: ageClass(pull.created_at, now) }, since(pull.created_at, now)),
        el("small", {}, "PR age"))),
    el("span", { class: "chevron", "aria-hidden": "true" }, "›"));
  }

  function renderPulls() {
    const pulls = flattenPulls(snapshot);
    content.replaceChildren(heading("Pull requests", "Open pull requests and current-head evidence from selected repositories.",
      coverageWarning(snapshot.github.coverage, snapshot.github)), prFilters(pulls), el("div", { class: "list-heading" }, el("span", { id: "pr-count" }),
        el("span", {}, "Current work"), el("span", {}, "Checks"), el("span", {}, "Age")),
      el("div", { id: "pr-rows", class: "pr-groups" }));
    renderPrRows();
  }

  function subscriptionPanel(accounts, now = Date.now()) {
    const panel = el("section", { class: "panel subscription-panel" });
    quotaGroups(accounts).forEach(group => {
      const block = el("div", { class: "subscription-group" });
      block.append(el("h2", {}, group.title));
      group.accounts.forEach(account => block.append(quota(account, now)));
      panel.append(block);
    });
    return panel;
  }

  function quota(account, now = Date.now()) {
    const panel = el("div", { class: "quota-lines" });
    if (!account.quota_windows.length) return panel.appendChild(el("span", { class: "muted" }, "Quota window unavailable"));
    account.quota_windows.map((window, index) => ({ window, index })).sort((a, b) => {
      const left = Number.isFinite(a.window.window_minutes) ? a.window.window_minutes : Infinity;
      const right = Number.isFinite(b.window.window_minutes) ? b.window.window_minutes : Infinity;
      return left - right || a.index - b.index;
    }).forEach(({ window }) => panel.append(quotaLine(window, now)));
    return panel;
  }

  function quotaLine(window, now) {
    const label = quotaWindowLabel(window);
    const used = quotaDisplayPercent(window.used_percent);
    const pace = quotaPace(window, now);
    const tone = quotaTone(window.used_percent, pace ? pace.delta : null);
    const countdown = quotaCountdown(window.resets_at, now);
    const delta = pace ? quotaDeltaLabel(pace.delta) : null;
    const usedText = used === null ? "usage unavailable" : `${used}% used`;
    const detail = [`${label}: ${usedText}`];
    if (pace) detail.push(quotaPacePhrase(pace));
    detail.push(`Resets ${formatTime(window.resets_at)}`);
    if (Number.isFinite(window.allowance_tokens)) detail.push(`${window.allowance_tokens.toLocaleString()} token allowance reported`);
    const track = el("div", {
      class: "quota-track", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100",
      "aria-valuenow": used === null ? "0" : String(used),
      "aria-label": [usedText, pace ? quotaPacePhrase(pace) : null, countdown].filter(Boolean).join(", ")
    });
    if (used) track.append(el("span", { class: `quota-fill tone-${tone}`, style: `width:${used}%` }));
    if (pace) track.append(el("span", {
      class: "quota-tick", style: `left:${pace.expected}%`, title: `Even pace: ${Math.round(pace.expected)}% used`
    }));
    const meta = [el("b", {}, used === null ? "Unavailable" : `${used}%`)];
    if (countdown) meta.push(` · ${countdown}`);
    if (delta) meta.push(" · ", el("span", { class: `quota-delta tone-${tone}` }, delta));
    return el("div", { class: "quota-line", title: detail.join(". ") },
      el("span", { class: "quota-label" }, label), track, el("span", { class: "quota-meta" }, ...meta));
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
    const bots = (snapshot.agents?.rows || []).map(agent => ({ name: agent.name, color: BOT_META[agent.role].color,
      burn: VVCharts.hourlyBurn(samples.filter(sample => sample.bot === agent.id), now, through) }));
    const observed = bots.map(bot => bot.burn).filter(Boolean);
    const loose = samples.filter(sample => !(snapshot.agents?.rows || []).some(agent => agent.id === sample.bot));
    const looseBurn = VVCharts.hourlyBurn(loose, now, through);
    const burns = [...observed, looseBurn].filter(Boolean);
    // With a sized plan, All bots is in percent of the plan an hour against the plan's even pace.
    const plan = usage.plan_line || null;
    const share = plan?.sized ? VVCharts.hourlyBurn(samples.filter(sample => Number.isFinite(sample.plan_percent)), now, through, sample => sample.plan_percent) : null;
    const all = { name: "All bots", color: "rgba(255,255,255,0.6)", all: true, looseTokens: looseBurn && !share ? looseBurn.last24h.reduce((total, value) => total + (value || 0), 0) : 0, burn: share || (burns.length ? VVCharts.sumBurns(burns) : null), percent: !!share, paceLine: plan ? plan.percent_per_hour : null,
      paceLineTitle: plan ? `Even pace to the reset: ${VVCharts.percentText(plan.percent_per_hour)} of the plan an hour spends the rest exactly when it resets.` : null, ...paceHeader(usage.pace_points ?? null) };
    const shared = VVCharts.ceiling(observed);
    return el("section", { class: "panel usage-charts" }, el("h2", {}, "Token burn pattern"),
      el("p", { class: "muted" }, "Tokens each bot used, hour by hour. Solid: last 24 hours. Dashed: a usual day. All bots: percent of the plan an hour; flat: the pace that lasts until the plan resets."),
      el("div", { class: "burn-cards" }, bots.map(bot => burnCard(bot, shared, now)),
        burnCard(all, VVCharts.ceiling(all.burn ? [all.burn] : [], all.paceLine), now)),
      plan ? el("p", { class: "muted pace-note", title: `Weighted the way the plan counts tokens (a cached input token a tenth of a fresh one, an output token five times), the bots' tokens since the window began made its ${plan.used_percent}% used; each hour is its share of that. This reads the bots' tokens as standing for all of the plan's use, which holds while they keep their share.${plan.partial ? " Token history is incomplete, so the bots' share may read high." : ""}` },
        `Flat line on All bots: ${VVCharts.percentText(plan.percent_per_hour)} of ${plan.plan} an hour uses the rest (${plan.window}, ${plan.used_percent}% used) exactly when it resets ${formatTime(plan.resets_at)}.${plan.sized ? "" : " No bot tokens are recorded on this plan's window yet, so All bots stays in tokens."}`) : null);
  }

  // One card: name and last-24h peak, the hour-of-day chart (or "Not yet observed"), and its totals.
  function burnCard(row, maximum, now) {
    const burn = row.burn, days = burn?.baselineDays ?? null;
    const card = el("article", { class: `burn-card${row.all ? " all-bots" : ""}`, style: `--accent:${row.color}` });
    const peak = Math.max(...(burn?.last24h || []).filter(Number.isFinite)), words = VVCharts.unitWords(row.percent);
    card.append(el("div", { class: "burn-head" }, el("b", { title: row.paceTitle }, row.pace ? `${row.name} · ${row.pace}` : row.name),
      el("span", { title: `Most ${words.most} in one clock hour of the last 24 hours` }, Number.isFinite(peak) ? `peak ${words.amount(peak)}/h` : null)));
    if (!burn) {
      card.append(el("p", { class: "burn-empty muted" }, "Not yet observed"));
      return card;
    }
    const usual = `prior ${days}-day hourly average`;
    const aside = row.looseTokens ? ` ${VVCharts.short(row.looseTokens)} tokens are not on a numbered bot.` : "";
    const method = VVCharts.method(words.lead, days) + aside;
    card.append(VVCharts.drawBurn(burn, { color: row.color, maximum, nowMs: now, usualTitle: `${row.name}: ${usual}`,
      label: `${row.name}: ${words.per} per clock hour, ${days === null ? "no usual day yet" : `against the ${usual}`}`,
      pace: row.paceLine, paceTitle: row.paceLineTitle }),
    el("div", { class: "burn-totals", title: method }, VVCharts.totals(burn, row.percent) + (row.looseTokens ? ` · ${VVCharts.short(row.looseTokens)} not on a numbered bot` : "")));
    return card;
  }

  function renderUsage() {
    const usage = snapshot.agents?.usage;
    content.replaceChildren(heading("Bot usage", "Measured tokens and subscription capacity."));
    if (usage?.available) {
      if (usage.stale) content.append(sourceBanner(`Usage telemetry is stale. Last sample: ${formatTime(usage.sampled_at)}.`, "warning"));
      const quotas = usage.accounts.filter(account => account.quota_windows.length);
      if (quotas.length) content.append(subscriptionPanel(quotas));
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
    const registrationText = { true: "Runner registered", false: "No runner registered" };
    const idleText = { provisionable: "Provisionable when work arrives", busy: "Busy; job details not yet matched",
      allocated: "Occupied; no job recorded for this slot" };
    capacity.lanes.forEach(lane => {
      const registration = registrationText[lane.registered];
      const card = el("article", { class: `lane status-${lane.state}` }, el("div", { class: "lane-heading" }, el("b", { class: "mono" }, lane.id), badge(lane.state)),
        registration ? el("p", { class: "muted" }, registration) : null);
      if (lane.job) card.append(el("div", {}, el("p", {}, lane.job.name), el("p", { class: "muted" }, lane.job.repository), link("Open job", lane.job.url, snapshot.owner)));
      else card.append(el("p", { class: "muted" }, idleText[lane.state] || "No current same-owner job"));
      if (lane.labels.length) card.append(el("small", { class: "muted" }, lane.labels.join(" · ")));
      grid.append(card);
    });
    const limits = capacity.limits;
    if (limits) lanes.prepend(el("p", { class: "muted lane-limits" },
      `${limits.slots} shared CI/QAE slots${limits.wait_slots ? ` · ${limits.wait_slots} wait slots` : ""} · QAE maximum ${limits.qae_concurrency} · CPU cap ${limits.cpu_quota_cores ?? "unknown"} cores · Memory cap ${bytes(limits.memory_max_bytes)}`));
    content.append(lanes);
  }

  function render() {
    if (!snapshot) return;
    renderChrome();
    const work = () => VVWork.render(content, snapshot, { heading, empty, sourceBanner });
    ({ work, usage: renderUsage, capacity: renderCapacity }[state.view] || renderPulls)();
    rememberView();
    const title = { prs: "Pull requests", work: "Work", usage: "Bot usage", capacity: "Capacity" }[state.view];
    document.title = `${title} · Vibe Verifier`;
    if (refreshFailed) content.prepend(sourceBanner("Could not refresh. Showing the last sample.", "warning"));
    announce(`Showing ${state.view}`);
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch("/api/dashboard", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("source unavailable");
      const next = await response.json();
      if (!snapshot) restoreView(next.owner);
      refreshFailed = false;
      snapshot = next;
      const focused = document.activeElement;
      const filterId = ["pr-search", "repo-filter"].includes(focused?.id) ? focused.id : null;
      const selection = filterId === "pr-search" ? [focused.selectionStart, focused.selectionEnd] : null;
      render();
      const replacement = filterId && document.getElementById(filterId);
      if (replacement) {
        replacement.focus({ preventScroll: true });
        if (selection) replacement.setSelectionRange(...selection);
      }
    } catch (_) {
      refreshFailed = true;
      if (snapshot) render();
      else {
        content.replaceChildren(empty("Dashboard unavailable", "The local read-only source could not be loaded."));
        document.querySelector("#bot-strip").replaceChildren(el("span", { class: "muted" }, "Bot status unavailable"));
        window.clearTimeout(retry); retry = window.setTimeout(refreshIfVisible, 5000); // One retry at a time, sooner than the 30-second poll.
      }
    } finally { loading = false; }
  }

  // A load can make the server re-read GitHub, whose hourly budget the signed-in account shares with
  // everything else it runs. A tab nobody can see skips its refresh and catches up when shown again.
  function refreshIfVisible() { if (!document.hidden) load(); }

  document.querySelectorAll(".sidebar button").forEach(button => button.addEventListener("click", () => go(button.dataset.view)));
  replaceUnknownHash();
  window.addEventListener("hashchange", show);
  window.addEventListener("pagehide", rememberView);
  load();
  window.setInterval(refreshIfVisible, 30000);
  document.addEventListener("visibilitychange", refreshIfVisible);
})();
