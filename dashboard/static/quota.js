// Plan quota windows for the Usage view: each window's name, shown percent, pace against an even burn,
// tone and countdown, and accounts grouped by provider. helpers.js re-exports every name, so callers
// read them from VV. Load this before helpers.js.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VVQuota = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

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

  return { quotaWindowLabel, quotaDisplayPercent, quotaPace, quotaPacePhrase, paceHeader, quotaDeltaLabel, quotaTone, quotaCountdown, quotaGroups };
});
