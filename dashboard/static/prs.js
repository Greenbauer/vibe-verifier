// The Pull requests view (docs/dashboard.md): open pull requests grouped by repository, each row with
// what it is doing, its checks or CI timeline, and its age. Read-only: it draws the snapshot and links
// out to GitHub.
(function () {
  "use strict";
  const { element: el, safeUrl, duration, ageLabel, since, ageClass, secondsClass, badge, flattenPulls, filterPulls,
    groupPulls, repositoriesWithPulls, coverageWarning, checkTotals, combinedCategory, meterSegments, meterLabel, runningWork,
    unresolvedMark, behindMark } = VV;
  const { ciTiming } = VVTimeline;
  // The pull request whose CI timeline is open, by "<repository>#<number>": at most one, so opening a
  // row closes any other. Not saved: a reload closes it.
  const expanded = new Set();
  let content, snapshot, state, heading, empty, rememberView;

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
    const scale = el("select", { id: "time-scale", "aria-label": "CI timeline scale", onchange: event => {
      state.timeScale = event.target.value; renderPrRows();
    } }, el("option", { value: "shared", selected: state.timeScale === "shared" }, "Same time scale for every row"),
      el("option", { value: "row", selected: state.timeScale === "row" }, "Fit each row's timeline"));
    return el("div", { class: "toolbar" }, search, select, scale, check("Subscribed only", "subscribed"), check("Needs attention", "attention"));
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

  // The row's checks. With a CI timeline (from rowView) the bars get the Checks cell to themselves and
  // the counts, CI time and push kind move to the CI column: two cells. Otherwise the check meter with
  // that caption under it, in one cell across both columns.
  function progress(pull, now = Date.now(), timeline = null) {
    const totals = checkTotals(pull);
    const mark = commentMark(pull);
    const segments = totals.known ? meterSegments(totals) : [];
    const open = segments.filter(segment => segment.state !== "success")
      .map(segment => `${segment.count} ${segment.label.toLowerCase()}`);
    const caption = el("div", { class: "progress-caption" },
      el("span", { class: "progress-counts" }, totals.known ? (open.join(" · ") || "All passed") : "Progress is not shown as complete"),
      pushLine(pull, now));
    if (totals.known && timeline) return [el("div", { class: "timeline-cell" }, timeline), el("div", { class: "progress-copy ci-copy" }, mark, caption)];
    const meter = !totals.known ? el("b", { class: "progress-lead" }, "No checks reported")
      : el("div", { class: "check-meter", role: "img", "aria-label": meterLabel(totals) });
    if (totals.known) segments.forEach(segment => meter.append(el("span", {
      class: `seg-${segment.state}`, style: `flex:${segment.count} 1 0`, title: `${segment.count} ${segment.label}`,
      "aria-hidden": "true"
    })));
    return el("div", { class: "progress-copy" },
      el("div", { class: mark ? "progress-layout has-mark" : "progress-layout" }, meter, mark, caption));
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
    const now = Date.now();
    const scale = state.timeScale === "row" ? null : VVTimeline.sharedScale(pulls, now);
    groupPulls(pulls).forEach(group => {
      const list = el("div", { class: "pr-list" });
      group.pulls.forEach(pull => {
        const view = rowView(pull, scale, now);
        list.append(prRow(pull, view));
        if (view.open) list.append(view.detail());
      });
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

  // A row's title opens the pull request on GitHub; the rest of the row, and the toggle at its end,
  // opens its CI timeline under it.
  // What the list gives a row about its CI timeline: the row's timeline on the list's scale (null to
  // fit the row), whether it is open, the toggle that opens or closes it, and the opened view.
  function rowView(pull, scale, now) {
    const key = `${pull.repository}#${pull.number}`;
    const id = `timeline-${pull.repository.replace(/[^\w-]/g, "-")}-${pull.number}`;
    return { key, id, open: expanded.has(key), timeline: VVTimeline.mini(pull, now, scale),
      toggle: () => {
        const opening = !expanded.has(key);
        expanded.clear();
        if (opening) expanded.add(key);
        renderPrRows();
        document.querySelector(`[data-focus-key="${CSS.escape(`timeline:${key}`)}"]`)?.focus({ preventScroll: true });
      },
      detail: () => VVTimeline.detail(pull, now, { id, key, owner: snapshot.owner, rerender: renderPrRows }) };
  }

  // A row's title opens the pull request on GitHub; the rest of the row, and the arrow at its end,
  // open its CI timeline under it. view is from rowView; without one the row shows the check meter.
  function prRow(pull, view = {}) {
    const category = combinedCategory(pull);
    const href = safeUrl(pull.html_url, snapshot.owner);
    const now = Date.now();
    const opened = href ? `Open ${pull.repository} pull request ${pull.number} on GitHub: ${pull.title}` : null;
    const reason = pull.attention_reason || "", ready = pull.merge_ready && !pull.stale;
    return el("div", { class: view.open ? "pr-row open" : "pr-row",
      onclick: view.toggle && (event => { if (!event.target.closest("a, button")) view.toggle(); }) },
    el("span", { class: "pr-identity" },
      el("b", { class: ready ? "merge-ready" : null, title: ready ? "Fully merge-ready" : null },
        href ? el("a", { class: "pr-title", href, target: "_blank", rel: "noreferrer",
          "aria-label": opened + (ready ? ". Fully merge-ready" : "") }, pull.title) : pull.title),
      el("small", {}, `#${pull.number} · ${pull.author || "unknown"} · ${pull.head_sha ? pull.head_sha.slice(0, 8) : "head changing"}`),
      // The bottom line: the age, then how far the head is behind its base, beside it.
      el("span", { class: "pr-meta" },
        el("span", { class: "pr-age", title: "How long the pull request has been open" },
          el("span", { class: "age-stat" }, el("b", { class: ageClass(pull.created_at, now) }, since(pull.created_at, now)),
            el("small", {}, "old"))),
        behindBadge(pull))),
    el("span", { class: "pr-work", title: reason || null }, badge(category), ...workDetail(pull)),
    progress(pull, now, view.timeline),
    el("button", { type: "button", class: "timeline-toggle", "aria-expanded": String(Boolean(view.open)), "aria-controls": view.id,
      "aria-label": `${view.open ? "Hide" : "Show"} the CI timeline of pull request ${pull.number}`,
      "data-focus-key": view.key && `timeline:${view.key}`, onclick: view.toggle }, el("span", { "aria-hidden": "true" }, "›")));
  }

  function renderPulls() {
    const pulls = flattenPulls(snapshot);
    content.replaceChildren(heading("Pull requests", null, coverageWarning(snapshot.github.coverage, snapshot.github), prFilters(pulls)),
      el("div", { class: "list-heading" }, el("span", { id: "pr-count" }),
        el("span", {}, "Current work"), el("span", {}, "Checks"), el("span", {}, "CI")),
      el("div", { id: "pr-rows", class: "pr-groups" }));
    renderPrRows();
  }

  // Draws the view into target. view is the page's filters and settings, kept and saved by app.js.
  function render(target, current, view, page) {
    content = target; snapshot = current; state = view;
    ({ heading, empty, rememberView } = page);
    renderPulls();
  }

  const api = { render };
  if (typeof module === "object" && module.exports) module.exports = api;
  else window.VVPrs = api;
})();
