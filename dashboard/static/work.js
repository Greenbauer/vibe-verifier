// The Work board (docs/dashboard.md "Work view"): plans and calls from the configured work sources,
// in four columns. Read-only: it renders what the server read and links out, nothing more.
(function () {
  "use strict";
  const { element: el, badge, since, ageClass, formatTime, httpsUrl, workGroups } = VV;

  const WORK_ERRORS = {
    credential_missing: "No credential file on this host yet.", credential_unsafe: "The credential file is not private to the dashboard account.",
    credential_unreadable: "The credential file cannot be read.", rejected: "The source refused the credential.",
    unreachable: "The source could not be reached.", unavailable: "The source answered with an error.",
    redirected: "The source answered with a redirect, which the dashboard does not follow.",
    invalid_response: "The source answered in a shape this dashboard does not read.", tool_error: "The source refused the request."
  };

  function externalLink(label, url) {
    const safe = httpsUrl(url);
    return safe ? el("a", { href: safe, class: "evidence-link", target: "_blank", rel: "noreferrer" }, label) : null;
  }

  function workText(text) { return el("div", { class: "work-text" }, text); }

  function workDetail(item, now) {
    if (!item.detail) return null;
    if (item.source_kind === "supabase-todos") return el("details", { class: "work-detail" }, el("summary", {}, "Notes"), workText(item.body));
    const parts = [];
    if (item.status_text) parts.push(el("p", { class: "muted" }, item.status_text));
    if (item.body) parts.push(el("h3", {}, "Request"), workText(item.body));
    if (item.refs.length) parts.push(el("p", { class: "muted" }, `Design references: ${item.refs.join(" · ")}`));
    const kind = item.source_kind === "calls" ? "Follow-up" : "Amendment";
    item.updates.forEach((update, index) => parts.push(
      el("h3", {}, `${kind} ${index + 1} · ${formatTime(update.created_at)} · ${since(update.created_at, now)} ago`), workText(update.body),
      update.refs.length ? el("p", { class: "muted" }, `Design references: ${update.refs.join(" · ")}`) : null));
    item.asks.forEach(ask => parts.push(
      el("h3", {}, `Ask · ${formatTime(ask.raised_at)}${ask.open ? " · awaiting a ruling" : ""}`), workText(ask.question),
      ask.ruling ? el("p", {}, el("b", {}, `Ruling (${formatTime(ask.ruled_at)}): `), ask.ruling) : null));
    if (!parts.length) return null;
    return el("details", { class: "work-detail" },
      el("summary", {}, `Request${item.updates.length ? ` and ${item.updates.length} ${kind.toLowerCase()}${item.updates.length === 1 ? "" : "s"}` : ""}${item.asks.length ? `, ${item.asks.length} ask${item.asks.length === 1 ? "" : "s"}` : ""}`),
      parts);
  }

  function workCard(item, now) {
    const updated = ageClass(item.updated_at, now);
    // A to-do has no claim; it has a due date.
    const todo = item.source_kind === "supabase-todos";
    const who = todo ? (item.due ? `due ${item.due}` : null) : [item.requester ? `from ${item.requester}` : null,
      item.claimed_by ? `claimed by ${item.claimed_by}` : (item.phase === "done" ? null : "unclaimed")].filter(Boolean).join(" · ");
    return el("article", { class: "work-card" },
      el("div", { class: "work-card-head" }, el("span", { class: "mono muted" }, item.label), badge(item.status)),
      el("div", { class: "work-title" }, item.item_url ? externalLink(item.title, item.item_url) : el("b", {}, item.title)),
      el("small", { class: "muted" }, [item.source, who].filter(Boolean).join(" · ")),
      el("small", {}, el("span", { class: "muted" }, `created ${since(item.created_at, now)} ago · `),
        el("span", { class: updated || "muted" }, `updated ${since(item.updated_at, now)} ago`)),
      item.waiting ? el("p", { class: "work-reason" }, item.waiting.reason) : null,
      el("div", { class: "work-links" },
        item.pr_number ? (externalLink(`PR #${item.pr_number}`, item.pr_url) || el("small", {}, `PR #${item.pr_number}`)) : null,
        externalLink("Preview", item.preview_url)),
      workDetail(item, now));
  }

  // One card per column, its items as rows inside it, like the pull-request groups.
  function workColumn(key, title, subtitle, items, now, collapsed = false) {
    const rows = items.length ? items.map(item => workCard(item, now)) : [el("p", { class: "work-empty muted" }, "None.")];
    return el("section", { class: `work-column work-${key}`, "aria-label": title },
      el("h2", { class: "work-column-title" }, `${title} (${items.length})`, el("small", {}, subtitle)),
      el("div", { class: "work-list" }, collapsed && items.length ? el("details", {}, el("summary", {}, "Show"), rows) : rows));
  }

  function render(content, snapshot, { heading, empty, sourceBanner }) {
    const work = snapshot.work || { configured: false, sources: [] };
    content.replaceChildren(heading("Work"));
    if (!work.configured) { content.append(empty("No work sources", "This dashboard has no work_sources configured.")); return; }
    const now = Date.now();
    work.sources.forEach(source => {
      const sampled = source.sampled_at ? `${source.label}: read ${formatTime(source.sampled_at)}` : source.label;
      if (source.loading) content.append(sourceBanner(`${source.label}: reading…`));
      else if (source.error) content.append(sourceBanner(`${sampled}. ${WORK_ERRORS[source.error] || "The source could not be read."}${source.available ? " Showing the last reading." : ""}`, "warning"));
      else if (source.stale) content.append(sourceBanner(`${sampled}. This reading is stale.`, "warning"));
      if (source.truncated) content.append(sourceBanner(`${source.label}: showing the oldest ${source.items.length} items only.`, "warning"));
    });
    const groups = workGroups(work, now);
    content.append(el("p", { class: "muted work-summary" },
      `${groups.todo.length} to do · ${groups.progress.length} in progress · ${groups.waiting.length} waiting on you · ${groups.stale} open items not updated in over two weeks`));
    content.append(el("div", { class: "work-board" },
      workColumn("todo", "To do", "Not picked up yet, oldest first", groups.todo, now),
      workColumn("progress", "In progress", "Claimed or being built", groups.progress, now),
      workColumn("waiting", "Waiting on you", "Blocked, an Ask without a ruling, a PR to merge, or a to-do marked waiting", groups.waiting, now),
      workColumn("done", "Done", "Merged, closed, or a to-do done", groups.done, now, true)));
  }

  window.VVWork = { render };
})();
