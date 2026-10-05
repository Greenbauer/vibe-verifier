(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VV = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const BOT_META = {
    swe: { color: "#809cff" },
    qae: { color: "#50d6e8" }
  };
  const STATUS_LABELS = {
    success: "Succeeded", failed: "Failed", skipped: "Skipped", cancelled: "Cancelled",
    pending: "Pending", unknown: "Unknown", working: "Working", idle: "Idle",
    paused: "Paused", down: "Down", ready: "Ready", busy: "Busy", provisionable: "On demand",
    offline: "Offline", allocated: "Allocated"
  };

  function restoreViewState(raw) {
    const state = { view: "prs", selected: null, query: "", repository: "all", subscribed: false,
      attention: false, failureBot: null };
    let saved;
    try { saved = JSON.parse(raw); } catch (_) { return state; }
    if (!saved || typeof saved !== "object") return state;
    if (["prs", "usage", "capacity"].includes(saved.view)) state.view = saved.view;
    for (const key of ["selected", "query", "repository", "failureBot"]) {
      if (typeof saved[key] === "string") state[key] = saved[key];
    }
    for (const key of ["subscribed", "attention"]) {
      if (typeof saved[key] === "boolean") state[key] = saved[key];
    }
    if (state.view !== "prs") state.selected = null;
    return state;
  }

  function element(tag, attrs, ...children) {
    const item = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === "class") item.className = value;
      else if (key === "text") item.textContent = value;
      else if (key === "style") value.split(";").forEach(rule => {
        const split = rule.indexOf(":");
        if (split > 0) item.style.setProperty(rule.slice(0, split).trim(), rule.slice(split + 1).trim());
      });
      else if (key.startsWith("on") && typeof value === "function") item.addEventListener(key.slice(2), value);
      else item.setAttribute(key, value === true ? "" : String(value));
    });
    children.flat().filter(value => value !== null && value !== undefined).forEach(child => {
      item.append(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return item;
  }

  function safeUrl(value, owner) {
    try {
      const url = new URL(value);
      const first = url.pathname.split("/").filter(Boolean)[0];
      return url.protocol === "https:" && url.hostname === "github.com" &&
        first && first.toLowerCase() === owner.toLowerCase() ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function link(label, url, owner, className) {
    const safe = safeUrl(url, owner);
    return safe ? element("a", { href: safe, class: className || "evidence-link", target: "_blank", rel: "noreferrer" }, label) : null;
  }

  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "Unavailable";
    const whole = Math.floor(seconds);
    if (whole < 60) return `${whole}s`;
    const minutes = Math.floor(whole / 60);
    if (minutes < 60) return `${minutes}m ${whole % 60}s`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ${minutes % 60}m`;
    return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  }

  function since(value, now) {
    const at = Date.parse(value);
    return Number.isFinite(at) ? duration(Math.max(0, (now - at) / 1000)) : "Unavailable";
  }

  function formatTime(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Unavailable";
  }

  function bytes(value) {
    if (!Number.isFinite(value)) return "Unavailable";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let amount = value;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
  }

  function badge(status, label) {
    return element("span", { class: `badge status-${status}`, text: label || STATUS_LABELS[status] || status });
  }

  // The capacity meters show how full a resource is. Disk telemetry reports free space, so derive used.
  function diskUsage(host) {
    const free = host.workspace_disk_free_bytes, total = host.workspace_disk_total_bytes;
    if (!Number.isFinite(free) || !Number.isFinite(total) || total <= 0) return null;
    const used = Math.max(0, total - free);
    return { used, total, percent: used / total * 100 };
  }

  function flattenPulls(snapshot) {
    return (snapshot.github.repositories || []).flatMap(repository =>
      (repository.pulls || []).map(pull => ({ ...pull, stale: repository.stale, source_error: repository.source_error }))
    );
  }

  function filterPulls(pulls, filters) {
    const query = filters.query.trim().toLowerCase();
    return pulls.filter(pull => {
      if (filters.repository !== "all" && pull.repository !== filters.repository) return false;
      if (filters.subscribed && pull.subscription !== "subscribed") return false;
      if (filters.attention && !pull.attention) return false;
      return !query || `${pull.repository} ${pull.number} ${pull.title} ${pull.author || ""}`.toLowerCase().includes(query);
    });
  }

  function stepTotals(pull) {
    const summaries = (pull.runs || []).map(run => run.step_summary);
    if (!summaries.length || summaries.some(summary => !summary || !summary.known)) {
      return { known: false, completed: null, total: null, remaining: null };
    }
    return summaries.reduce((total, summary) => ({
      known: true,
      completed: total.completed + summary.completed,
      total: total.total + summary.total,
      remaining: total.remaining + summary.remaining
    }), { known: true, completed: 0, total: 0, remaining: 0 });
  }

  function combinedCategory(pull) {
    // A skipped check is not a result, so it never outranks a pass; "skipped" shows only when every check skipped.
    const order = ["failed", "cancelled", "pending", "unknown", "success", "skipped"];
    const categories = [...(pull.checks || []), ...(pull.statuses || [])].map(row => row.category);
    return order.find(value => categories.includes(value)) || "unknown";
  }

  function currentWork(pull) {
    for (const run of pull.runs || []) {
      for (const job of run.jobs || []) {
        const step = (job.steps || []).find(row => row.status === "in_progress");
        if (step) return { name: `${job.name}: ${step.name}`, elapsed: step.elapsed_seconds };
        if (job.category === "pending") return { name: job.name, elapsed: job.elapsed_seconds };
      }
    }
    return null;
  }

  return { BOT_META, STATUS_LABELS, element, safeUrl, link, duration, since, formatTime, bytes,
    badge, diskUsage, flattenPulls, filterPulls, stepTotals, combinedCategory, currentWork, restoreViewState };
});
