// The CI timeline's time axis (timeline.js): a lane's position on a span, and at most six ticks on
// round times. Load this before timeline.js.
(function () {
  "use strict";
  const el = VV.element;
  const TICKS = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400];

  const percent = (ms, width) => `${(Math.min(1, Math.max(0, ms / width)) * 100).toFixed(3)}%`;

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

  const api = { percent, ticks, axis };
  if (typeof module === "object" && module.exports) module.exports = api;
  else window.VVTimelineAxis = api;
})();
