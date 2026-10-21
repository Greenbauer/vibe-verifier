(function () {
  "use strict";
  const { BOT_META, element: el, link, duration, since, formatTime, badge, botState, sourceStamp,
    quotaWindowLabel, quotaDisplayPercent, quotaPace, quotaPacePhrase, paceHeader, quotaDeltaLabel, quotaTone, quotaCountdown, quotaGroups } = VV;
  const content = document.querySelector("#content");
  const announcement = document.querySelector("#announcement");
  const state = { ...restoreViewState(null), ...VV.parseRoute(location.hash) };
  let snapshot = null;
  let loading = false, retry = 0;
  let refreshFailed = false;

  // localStorage, not sessionStorage: a reload, a new tab or a reopened window all keep the filters.
  function restoreView(owner) {
    try { Object.assign(state, restoreViewState(localStorage.getItem(`vv-dashboard-view:${owner}`))); }
    catch (_) { /* Storage can be disabled by the browser. */ }
  }

  // Filters and the selected bot, saved in this browser for every tab. Where you are lives in the URL instead.
  function restoreViewState(raw) {
    const state = { query: "", repository: "all", subscribed: false, attention: false, failureBot: null };
    let saved;
    try { saved = JSON.parse(raw); } catch (_) { return state; }
    if (!saved || typeof saved !== "object") return state;
    for (const key of ["query", "repository", "failureBot"]) {
      if (typeof saved[key] === "string") state[key] = saved[key];
    }
    for (const key of ["subscribed", "attention"]) {
      if (typeof saved[key] === "boolean") state[key] = saved[key];
    }
    return state;
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
  // A page title with no description under it; a warning, when there is one, takes the line under the
  // title. Controls follow the title on the left and wrap below it when narrow.
  function heading(title, warning, controls) {
    const line = warning
      ? el("p", { class: "title-warning", role: "status" }, el("i", { class: "warning-dot", "aria-hidden": "true" }), warning) : null;
    return el("div", { class: "section-heading" }, el("div", { class: "section-title" }, el("h1", {}, title), line), controls);
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
    const panel = el("section", { class: "panel failures" }, el("h2", {}, "Recent bot failures"));
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
    content.replaceChildren(heading("Bot usage"));
    if (usage?.available) {
      if (usage.stale) content.append(sourceBanner(`Usage telemetry is stale. Last sample: ${formatTime(usage.sampled_at)}.`, "warning"));
      const quotas = usage.accounts.filter(account => account.quota_windows.length);
      if (quotas.length) content.append(subscriptionPanel(quotas));
      content.append(usageCharts(usage), el("p", { class: "muted coverage-note" }, usage.completeness));
    } else content.append(usageCharts({ samples: [], accounts: [] }), sourceBanner("Usage source is unavailable.", "warning"));
    content.append(failurePanel());
  }

  function render() {
    if (!snapshot) return;
    renderChrome();
    const work = () => VVWork.render(content, snapshot, { heading, empty, sourceBanner });
    const capacity = () => VVCapacity.render(content, snapshot, { heading, empty, sourceBanner, rerender: render });
    const prs = () => VVPrs.render(content, snapshot, state, { heading, empty, rememberView });
    ({ work, usage: renderUsage, capacity }[state.view] || prs)();
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
      const focusKey = focused?.dataset?.focusKey;
      render();
      const replacement = (filterId && document.getElementById(filterId)) ||
        (focusKey && document.querySelector(`[data-focus-key="${CSS.escape(focusKey)}"]`));
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
