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
    pending: "Pending", running: "Running", waiting: "Waiting", unknown: "Unknown", working: "Working", idle: "Idle",
    paused: "Paused", down: "Down", ready: "Ready", busy: "Busy", offline: "Offline",
    submitted: "Queued", backlog: "Backlog", in_progress: "In progress", in_review: "In review",
    pr_ready: "PR ready", await_answer: "Awaiting answer", blocked: "Blocked", merged: "Merged", closed: "Closed",
    todo: "To do", done: "Done"
  };

  // The URL hash is the one record of the current view, so refresh, back and forward, and a copied
  // link all land in the same place: #/prs, #/usage, or #/capacity. A pull request opens on GitHub,
  // so an older #/pr/<owner>/<repo>/<number> hash is not a view and falls back to the list.
  function parseRoute(hash) {
    const view = /^#\/(\w+)$/.exec(hash || "")?.[1];
    return { view: ["prs", "work", "usage", "capacity"].includes(view) ? view : "prs" };
  }

  function routeHash(route) {
    return `#/${route.view}`;
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

  // A link the work view may open: HTTPS only, any host, since a work source names its own app.
  function httpsUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  // The work board's columns, each oldest first, every item carrying its source's label. An open item
  // that waits on a person is in `waiting` only; otherwise a queued one is to do and the rest in progress.
  function workGroups(work, now) {
    const groups = { todo: [], progress: [], waiting: [], done: [], stale: 0 };
    (work?.sources || []).forEach(source => (source.items || []).forEach(item => {
      const row = { ...item, source: source.label, source_kind: source.kind };
      const column = row.phase === "done" ? "done" : row.waiting ? "waiting" : row.phase === "queued" ? "todo" : "progress";
      groups[column].push(row);
      if (column !== "done" && ageClass(row.updated_at, now) === "age-red") groups.stale += 1;
    }));
    const order = (a, b) => String(a.created_at).localeCompare(String(b.created_at));
    ["todo", "progress", "waiting", "done"].forEach(key => groups[key].sort(order));
    return groups;
  }

  function link(label, url, owner, className) {
    const safe = safeUrl(url, owner);
    return safe ? element("a", { href: safe, class: className || "evidence-link", target: "_blank", rel: "noreferrer" }, label) : null;
  }

  const HOUR = 60 * 60;
  const DAY = 24 * HOUR;
  const WEEK = 7 * DAY;
  const MONTH = 30 * DAY;
  const YEAR = 365 * DAY;

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

  function elapsedSeconds(value, now) {
    const at = Date.parse(value);
    const seconds = (now - at) / 1000;
    return Number.isFinite(at) && Number.isFinite(seconds) ? Math.max(0, seconds) : null;
  }

  // Open-pull age. Two units under a day, then one: days through a week, weeks through
  // thirty days, months until a year, then years. An exact three-day or two-week age
  // keeps the younger color.
  function ageLabel(seconds) {
    const whole = Math.floor(seconds);
    if (whole < 60) return `${whole}s`;
    const minutes = Math.floor(whole / 60);
    if (minutes < 60) return `${minutes}m ${whole % 60}s`;
    const hours = Math.floor(minutes / 60);
    if (whole < DAY) return `${hours}h ${minutes % 60}m`;
    if (whole <= WEEK) return `${Math.floor(whole / DAY)}d`;
    if (whole <= MONTH) return `${Math.floor(whole / WEEK)}w`;
    if (whole < YEAR) return `${Math.floor(whole / MONTH)}mo`;
    return `${Math.floor(whole / YEAR)}y`;
  }

  function since(value, now) {
    const seconds = elapsedSeconds(value, now);
    return seconds == null ? "Unavailable" : ageLabel(seconds);
  }

  // Seconds after which an age turns yellow, then red. A pull request's open age is slow;
  // the time since its last push is not, and a CI run slower still is a stuck one.
  const AGE_LIMITS = {
    pull: { yellow: 3 * DAY, red: 2 * WEEK },
    push: { yellow: 3 * HOUR, red: DAY },
    ci: { yellow: HOUR, red: 3 * HOUR }
  };

  function secondsClass(seconds, kind) {
    const limits = AGE_LIMITS[kind];
    if (seconds == null || seconds <= limits.yellow) return null;
    return seconds > limits.red ? "age-red" : "age-yellow";
  }

  function ageClass(value, now, kind = "pull") {
    return secondsClass(elapsedSeconds(value, now), kind);
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

  // A bot's state as last read. The server keeps every source current, so no age is shown.
  function botState(row) {
    const state = row.state || "unknown";
    return { status: state, label: STATUS_LABELS[state] || state };
  }

  // The header's line about GitHub: when the reading on screen was taken.
  function sourceStamp(github) {
    if (!github.sampled_at) return github.refreshing ? "Fetching GitHub data…" : "GitHub unavailable";
    return `GitHub sampled ${formatTime(github.sampled_at)}`;
  }

  // The capacity meters show how full a resource is. Disk telemetry reports free space, so derive used.
  function diskUsage(host) {
    const free = host.workspace_disk_free_bytes, total = host.workspace_disk_total_bytes;
    if (!Number.isFinite(free) || !Number.isFinite(total) || total <= 0) return null;
    const used = Math.max(0, total - free);
    return { used, total, percent: used / total * 100 };
  }

  function repositoriesWithPulls(pulls) {
    return [...new Set(pulls.map(pull => pull.repository))].sort();
  }

  // Nothing to say while the list is fine or still being read: the header stamp covers reading.
  // selected null after a finished read means the owner-wide list is unknown, not empty.
  function coverageWarning(coverage, github) {
    if (coverage.selected == null) return github && github.refreshing ? null : "Repository list is unavailable.";
    return ((github && github.errors) || []).length ? "Some GitHub data is unavailable." : null;
  }

  function flattenPulls(snapshot) {
    return (snapshot.github.repositories || []).flatMap(repository =>
      (repository.pulls || []).map(pull => ({ ...pull, stale: Boolean(pull.stale || repository.stale), source_error: repository.source_error }))
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

  // One group per repository that has pulls, newest activity first. Within a group the oldest pull is last.
  function groupPulls(pulls) {
    const time = value => { const at = Date.parse(value); return Number.isFinite(at) ? at : -Infinity; };
    const groups = new Map();
    pulls.forEach(pull => {
      if (!groups.has(pull.repository)) groups.set(pull.repository, { repository: pull.repository, pulls: [], activity: -Infinity });
      const group = groups.get(pull.repository);
      group.pulls.push(pull);
      group.activity = Math.max(group.activity, time(pull.updated_at), time(pull.created_at));
    });
    const result = [...groups.values()];
    result.forEach(group => group.pulls.sort((a, b) => time(b.created_at) - time(a.created_at) || b.number - a.number));
    return result.sort((a, b) => b.activity - a.activity || a.repository.localeCompare(b.repository));
  }

  // GitHub's pull request page counts checks: one per check run, commit status and expected required check.
  // The check line uses the same buckets, left to right: passed, running, waiting, failed, then the gray ones.
  const METER_STATES = ["success", "running", "waiting", "failed", "skipped", "cancelled", "unknown"];

  // GitHub marks a started check in_progress. A queued job, a pending status and an expected check all wait.
  function checkState(row) {
    if (row.category === "pending") return row.status === "in_progress" ? "running" : "waiting";
    return METER_STATES.includes(row.category) ? row.category : "unknown";
  }

  function checkTotals(pull) {
    const counts = Object.fromEntries(METER_STATES.map(state => [state, 0]));
    const rows = [...(pull.checks || []), ...(pull.statuses || []), ...(pull.expected || [])];
    rows.forEach(row => { counts[checkState(row)] += 1; });
    const open = counts.running + counts.waiting + counts.unknown;
    // A head with no checks is not a finished pull request.
    return { known: rows.length > 0, completed: rows.length - open, total: rows.length, remaining: open, counts };
  }

  function combinedCategory(pull) {
    // A skipped check is not a result, so it never outranks a pass; "skipped" shows only when every check skipped.
    // A pull request never detailed has no rows; the cheap pass's rollup is the badge until detail arrives.
    const rows = [...(pull.checks || []), ...(pull.statuses || []), ...(pull.expected || [])];
    if (!rows.length && ["success", "failed", "pending"].includes(pull.rollup_category)) return pull.rollup_category;
    const order = ["failed", "cancelled", "pending", "unknown", "success", "skipped"];
    return order.find(value => rows.some(row => row.category === value)) || "unknown";
  }

  function meterSegments(totals) {
    if (!totals || !totals.known || !totals.total || !totals.counts) return [];
    return METER_STATES.flatMap(state => {
      const count = Number(totals.counts[state]) || 0;
      return count > 0 ? [{ state, count, label: STATUS_LABELS[state] }] : [];
    });
  }

  function meterLabel(totals) {
    const segments = meterSegments(totals);
    if (!totals || !totals.known) return "No checks reported";
    const parts = segments.map(segment => `${segment.count} ${segment.label.toLowerCase()}`);
    return `${parts.join(", ")} of ${totals.total} checks`;
  }

  // Every check GitHub marks in progress. An Actions job shares its check run's id, so its running step names the work.
  function runningWork(pull) {
    const jobs = new Map((pull.runs || []).flatMap(run => (run.jobs || []).map(job => [job.id, job])));
    return (pull.checks || []).filter(row => row.status === "in_progress").map(row => {
      const step = ((jobs.get(row.id) || {}).steps || []).find(item => item.status === "in_progress");
      return step ? { name: `${row.name}: ${step.name}`, elapsed: step.elapsed_seconds }
        : { name: row.name, elapsed: row.elapsed_seconds };
    });
  }

  // A message icon is shown only when at least one review thread is still unresolved.
  // Zero, and a read that never arrived, stay hidden.
  function unresolvedMark(pull) {
    const unresolved = pull.unresolved_comments;
    const threads = Number.isInteger(pull.review_threads) ? pull.review_threads : 0;
    const comments = Number.isInteger(pull.comment_count) ? pull.comment_count : 0;
    if (!Number.isInteger(unresolved) || unresolved < 1 || threads + comments <= 0) return null;
    const complete = pull.comments_complete !== false;
    const noun = unresolved === 1 && complete ? "unresolved comment" : "unresolved comments";
    return {
      shown: complete ? String(unresolved) : `${unresolved}+`,
      title: complete ? `${unresolved} ${noun}` : `${unresolved}+ unresolved comments`,
      unresolved
    };
  }

  // Shown only when the default branch has commits this head does not. Up to date, a fork, a
  // pull request into another branch, and a reading that never arrived stay hidden.
  function behindMark(pull) {
    const count = pull.behind_by;
    if (!Number.isInteger(count) || count < 1) return null;
    const branch = typeof pull.default_branch === "string" && pull.default_branch ? pull.default_branch : "main";
    return {
      shown: `${count} behind ${branch}`,
      title: `${branch} has ${count} ${count === 1 ? "commit" : "commits"} this branch does not`
    };
  }

  // A quota window is named from its length, so a 5-hour limit and a 7-day limit read as such
  // whatever the source called them. A name is kept when the length was not reported.
  function quotaWindowLabel(window) {
    const minutes = window.window_minutes;
    if (!Number.isFinite(minutes) || minutes <= 0) return window.name || "Window";
    if (minutes % 1440 === 0) {
      const days = minutes / 1440;
      return days === 1 ? "1 day" : `${days} days`;
    }
    if (minutes % 60 === 0) {
      const hours = minutes / 60;
      return hours === 1 ? "1 hour" : `${hours} hours`;
    }
    return `${minutes} min`;
  }

  function quotaDisplayPercent(used) {
    if (!Number.isFinite(used)) return null;
    return Math.round(Math.min(100, Math.max(0, used)));
  }

  // Points the fill sits ahead of an even burn, the tick on the usage bar:
  // used percent minus the share of the window already elapsed. Null until the window has started.
  function quotaPace(window, now) {
    const minutes = window.window_minutes;
    const used = window.used_percent;
    const reset = Date.parse(window.resets_at);
    if (!Number.isFinite(minutes) || minutes <= 0 || !Number.isFinite(used) || !Number.isFinite(reset)) return null;
    const remaining = (reset - now) / 1000;
    if (remaining <= 0) return null;
    const elapsed = 1 - remaining / (minutes * 60);
    if (!(elapsed > 0 && elapsed <= 1)) return null;
    return { expected: elapsed * 100, delta: Math.min(used, 100) - elapsed * 100 };
  }

  function quotaPacePhrase(pace) {
    const even = Math.round(pace.expected);
    const points = Math.round(Math.abs(pace.delta));
    const noun = points === 1 ? "point" : "points";
    if (pace.delta > 1) return `${points} ${noun} ahead of an even ${even}% burn`;
    if (pace.delta < -1) return `${points} ${noun} behind an even ${even}% burn`;
    return `on pace with an even ${even}% burn`;
  }

  // "12% under pace" for a card's header, with its hover: a plan window's fill against an even burn
  // of it, in points. A null delta (no window to pace) says nothing.
  function paceHeader(delta) {
    if (delta === null) return {};
    if (delta > 1) return { pace: `${Math.round(delta)}% ahead of pace`,
      paceTitle: `${Math.round(delta)} points ahead of an even burn: spending the plan faster than its window resets.` };
    if (delta < -1) return { pace: `${Math.round(-delta)}% under pace`,
      paceTitle: `${Math.round(-delta)} points behind an even burn: headroom.` };
    return { pace: "on pace", paceTitle: "On pace with the plan's reset window." };
  }

  // Shown only once the fill is more than a point off an even burn.
  function quotaDeltaLabel(delta) {
    if (!Number.isFinite(delta) || Math.abs(delta) <= 1) return null;
    const points = Math.round(delta);
    if (points === 0) return null;
    return `${points > 0 ? "+" : ""}${points}%`;
  }

  // Green while there is room and the burn is not ahead. Yellow from half full, or from being
  // ahead of an even burn. Red from 90% full or 25 points ahead.
  function quotaTone(usedPercent, delta) {
    const used = quotaDisplayPercent(usedPercent);
    if (used === null) return "ok";
    if (used >= 90 || (Number.isFinite(delta) && delta >= 25)) return "hot";
    if (used >= 50 || (Number.isFinite(delta) && delta > 1)) return "warn";
    return "ok";
  }

  function quotaCountdown(resetsAt, now) {
    const reset = Date.parse(resetsAt);
    if (!Number.isFinite(reset)) return null;
    const minutes = Math.floor((reset - now) / 60000);
    if (minutes <= 0) return "reset";
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor((minutes % 1440) / 60);
    const mins = minutes % 60;
    if (days > 0) return hours > 0 ? `resets ${days}d ${hours}h` : `resets ${days}d`;
    if (hours > 0) return mins > 0 ? `resets ${hours}h ${mins}m` : `resets ${hours}h`;
    return `resets ${minutes}m`;
  }

  const QUOTA_PROVIDERS = { openai: "OpenAI", anthropic: "Anthropic" };

  function quotaGroupTitle(provider, account) {
    const name = (provider || "").trim();
    if (!name) return account.label || "Subscription";
    const mapped = QUOTA_PROVIDERS[name.toLowerCase()] || name;
    return /usage$/i.test(mapped) ? mapped : `${mapped} usage`;
  }

  // One block per provider. Accounts that share a provider are models of that subscription.
  function quotaGroups(accounts) {
    const groups = [];
    accounts.forEach(account => {
      const provider = typeof account.provider === "string" ? account.provider.trim() : "";
      const key = provider.toLowerCase() || `label:${(account.label || account.id || "").toLowerCase()}`;
      let group = groups.find(item => item.key === key);
      if (!group) {
        group = { key, title: quotaGroupTitle(provider, account), accounts: [] };
        groups.push(group);
      }
      group.accounts.push(account);
    });
    return groups;
  }

  return { BOT_META, STATUS_LABELS, element, safeUrl, link, duration, ageLabel, since, elapsedSeconds, ageClass, secondsClass, formatTime, bytes,
    badge, botState, sourceStamp, diskUsage, flattenPulls, filterPulls, groupPulls, repositoriesWithPulls, coverageWarning, checkTotals, combinedCategory, checkState, meterSegments, meterLabel, runningWork, unresolvedMark, behindMark,
    parseRoute, routeHash, httpsUrl, workGroups, quotaWindowLabel, quotaDisplayPercent, quotaPace, quotaPacePhrase, paceHeader,
    quotaDeltaLabel,
    quotaTone, quotaCountdown, quotaGroups };
});
