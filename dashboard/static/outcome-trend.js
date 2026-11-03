// The Outcomes view's trend (docs/dashboard.md "Outcomes view"): each timer's median per day over the
// last 7, 14 or 30 days. One small chart per timer, so a runner wait of seconds and a merge wait of
// hours each keep a scale of their own instead of sharing one axis.
(function () {
  "use strict";
  const { element: el, duration } = VV;
  const charts = typeof module === "object" && module.exports ? require("./charts.js") : window.VVCharts;
  const RANGES = { "7d": 7, "14d": 14, "30d": 30 };
  const SERIES = [
    { key: "wait", label: "Waiting for a runner", value: row => row.round?.wait_seconds },
    { key: "running", label: "Checks running", value: row => row.round?.running_seconds },
    { key: "merge", label: "Waiting on the merge", value: row => row.merge_wait_seconds },
  ];
  const W = 280, H = 96, PAD = 4, PAD_T = 8, PAD_B = 14, COLOR = "#809cff";
  let chosen = "30d";

  const svg = (tag, attrs, text) => {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs).forEach(([key, value]) => item.setAttribute(key, String(value)));
    if (text !== undefined) item.textContent = text;
    return item;
  };
  // Local midnight `back` calendar days before today's.
  const midnight = (now, back) => { const day = new Date(now); day.setHours(0, 0, 0, 0); day.setDate(day.getDate() - back); return day.getTime(); };
  const dayLabel = time => new Date(time).toLocaleDateString([], { month: "short", day: "numeric" });

  // The range's days, oldest first: each one's start, how many pull requests merged on it, and per
  // timer their median (null on a day with none to measure).
  function days(rows, count, now, median) {
    return Array.from({ length: count }, (_, index) => {
      const back = count - 1 - index, start = midnight(now, back), end = midnight(now, back - 1);
      const merged = rows.filter(row => { const time = Date.parse(row.record.merged_at); return time >= start && time < end; });
      return { start, merged: merged.length, ...Object.fromEntries(SERIES.map(series => [series.key, median(merged.map(series.value))])) };
    });
  }

  function tip(series, day) {
    const value = day[series.key];
    const merged = `${day.merged} merged pull ${day.merged === 1 ? "request" : "requests"}`;
    if (!day.merged) return `${dayLabel(day.start)}: no merged pull requests`;
    return value == null ? `${dayLabel(day.start)}: ${merged}, none with this time to measure`
      : `${dayLabel(day.start)}: median ${duration(value)} over ${merged}`;
  }

  // One timer's line: a break on a day with nothing merged, and a short stroke for a day alone.
  function chart(series, list) {
    const values = list.map(day => day[series.key]);
    const top = Math.max(60, ...values.filter(Number.isFinite));
    const band = { min: PAD_T, max: H - PAD_B };
    const x = index => PAD + (W - 2 * PAD) * (index + 0.5) / list.length;
    const y = value => band.max - (band.max - band.min) * value / top;
    const plot = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, preserveAspectRatio: "none", role: "img",
      "aria-label": `${series.label}, median per day, up to ${duration(top)}` });
    const half = (W - 2 * PAD) / list.length / 2;
    plot.append(svg("line", { class: "burn-hourline", x1: PAD, x2: W - PAD, y1: band.max, y2: band.max }));
    charts.runs(values, 0, values.length - 1).forEach(run => {
      const points = run.length > 1 ? run.map(point => ({ x: x(point.hour), y: y(point.value) }))
        : [{ x: x(run[0].hour) - half / 2, y: y(run[0].value) }, { x: x(run[0].hour) + half / 2, y: y(run[0].value) }];
      // Straight between days: a curve would draw medians between them that no day had.
      const d = points.map((point, at) => `${at ? "L" : "M"} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
      plot.append(svg("path", { class: "burn-line", stroke: COLOR, d }));
    });
    list.forEach((day, index) => {
      const hit = svg("rect", { class: "trend-hit", x: x(index) - half, y: 0, width: 2 * half, height: H });
      hit.append(svg("title", {}, tip(series, day)));
      plot.append(hit);
    });
    const frame = el("div", { class: "burn-chart" }, plot);
    [[0, dayLabel(list[0].start)], [list.length - 1, "today"]].forEach(([index, text]) => {
      const label = el("span", { class: "burn-hour" }, text);
      label.style.left = `${x(index) / W * 100}%`;
      frame.append(label);
    });
    return { frame, top };
  }

  // overall is the timer's median over every pull request merged in the range.
  function card(series, list, overall) {
    const { frame, top } = chart(series, list);
    return el("article", { class: "burn-card", style: `--accent:${COLOR}` },
      el("div", { class: "burn-head" }, el("b", {}, series.label)), frame,
      el("div", { class: "burn-totals" }, `${overall == null ? "Nothing merged to measure" : `Median over the range ${duration(overall)}`} · chart max ${duration(top)}`));
  }

  // The trend panel: the range buttons, then one card per timer. rows are the view's outcomes.
  function panel(rows, now, median, rerender) {
    const buttons = el("div", { class: "range-buttons", role: "group", "aria-label": "Time range" },
      ...Object.keys(RANGES).map(name => el("button", { class: name === chosen ? "active" : null,
        "aria-pressed": name === chosen ? "true" : "false", onclick: () => { chosen = name; rerender(); } }, name)));
    const list = days(rows, RANGES[chosen], now, median);
    const until = midnight(now, -1);
    const inRange = rows.filter(row => { const time = Date.parse(row.record.merged_at); return time >= list[0].start && time < until; });
    return el("section", { class: "panel outcome-trend" },
      el("div", { class: "panel-title" }, el("h2", {}, "TRENDS"), buttons),
      el("p", { class: "muted" }, `Median per day of the pull requests merged that day, last ${RANGES[chosen]} days`),
      el("div", { class: "burn-cards" }, ...SERIES.map(series => card(series, list, median(inRange.map(series.value))))));
  }

  const api = { panel, days, RANGES, choose: name => { chosen = name; } };
  if (typeof module === "object" && module.exports) module.exports = api;
  else window.VVOutcomeTrend = api;
})();
