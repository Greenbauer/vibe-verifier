(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VVCharts = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  // One hour-of-day chart in viewBox units. The SVG stretches to its card's width.
  const W = 280, H = 126, PAD_L = 4, PAD_R = 4, PAD_T = 8, PAD_B = 16;
  const INNER_W = W - PAD_L - PAD_R, INNER_H = H - PAD_T - PAD_B;
  const HOUR = 3600000, PRIOR_DAYS = 6, LABELLED_HOURS = [0, 6, 12, 18];

  const sum = values => values.reduce((total, value) => total + (value || 0), 0);

  // "12a" / "6a" / "12p" / "6p": a compact clock-hour label.
  function hourLabel(hour) {
    return `${hour % 12 === 0 ? 12 : hour % 12}${hour < 12 ? "a" : "p"}`;
  }

  // Tokens per hour slot, where slot k is the k-th local clock hour before the current one (slot 0).
  function slotTotals(samples, nowMs, hourTop) {
    const slotOf = time => Math.max(0, Math.ceil((hourTop - time) / HOUR));
    const cells = new Array((PRIOR_DAYS + 1) * 24).fill(0);
    let first = Infinity, last = -Infinity;
    for (const sample of samples || []) {
      const time = Date.parse(sample.timestamp);
      const amount = Number(sample.input_tokens) + Number(sample.output_tokens);
      if (!Number.isFinite(time) || !Number.isFinite(amount) || time > nowMs || slotOf(time) >= cells.length) continue;
      cells[slotOf(time)] += amount;
      first = Math.min(first, time);
      last = Math.max(last, time);
    }
    return { cells, slotOf, first, last };
  }

  /**
   * One bot's tokens per local clock hour, indexed 0 (midnight) to 23.
   *
   * last24h is the current clock hour and the 23 before it. avg is that clock hour averaged over
   * the prior days, only where they were observed, and baselineDays is the fewest days any of its
   * hours averages. An hour is observed from the hour of the bot's first retained sample through
   * the newest observation (the source's sample time, or a later sample). Inside that span an hour
   * with no sample is a measured zero; outside it the hour is null, never zero. A bot with no
   * sample returns null.
   */
  function hourlyBurn(samples, nowMs, observedThroughMs) {
    const top = new Date(nowMs);
    top.setMinutes(0, 0, 0);
    const { cells, slotOf, first, last } = slotTotals(samples, nowMs, top.getTime());
    if (first === Infinity) return null;
    const through = Math.max(last, Number.isFinite(observedThroughMs) ? Math.min(nowMs, observedThroughMs) : nowMs);
    const oldest = slotOf(first), newest = slotOf(through);
    const value = slot => (slot >= newest && slot <= oldest ? cells[slot] : null);
    const last24h = new Array(24).fill(null), avg = new Array(24).fill(null), days = [];
    for (let slot = 0; slot < 24; slot++) {
      const hour = (((top.getHours() - slot) % 24) + 24) % 24;
      last24h[hour] = value(slot);
      const prior = Array.from({ length: PRIOR_DAYS }, (_, day) => value(slot + 24 * (day + 1))).filter(v => v !== null);
      if (!prior.length) continue;
      avg[hour] = sum(prior) / prior.length;
      days.push(prior.length);
    }
    return { last24h, avg, baselineDays: days.length ? Math.min(...days) : null };
  }

  // All bots: each hour adds the bots observed in it. An hour no bot observed stays null.
  function sumBurns(burns) {
    const add = (key, hour) => {
      const values = burns.map(burn => burn[key][hour]).filter(value => value !== null);
      return values.length ? sum(values) : null;
    };
    const hours = Array.from({ length: 24 }, (_, hour) => hour);
    const days = burns.map(burn => burn.baselineDays).filter(value => value !== null);
    return { last24h: hours.map(hour => add("last24h", hour)), avg: hours.map(hour => add("avg", hour)),
      baselineDays: days.length ? Math.min(...days) : null };
  }

  // The y ceiling: the bot cards pass all their burns so they share one scale; All bots passes its own.
  function ceiling(burns, pace) {
    const values = burns.flatMap(burn => [...burn.last24h, ...burn.avg]).filter(Number.isFinite);
    return Math.max(1, Number.isFinite(pace) ? pace : 0, ...values);
  }

  /**
   * Catmull-Rom points as cubic Beziers (tension 1/6), so hour to hour reads as a soft curve.
   * A cubic Bezier stays inside the hull of its control points, so clamping both control Ys into
   * the plot band keeps the curve from dipping below the zero baseline between a zero and a spike.
   */
  function smoothPath(points, band) {
    const round = n => Math.round(n * 10) / 10;
    const clamp = y => Math.max(band.min, Math.min(band.max, y));
    if (!points.length) return "";
    let path = `M ${round(points[0].x)} ${round(points[0].y)}`;
    for (let index = 0; index < points.length - 1; index++) {
      const p0 = points[index - 1] || points[index], p1 = points[index], p2 = points[index + 1];
      const p3 = points[index + 2] || p2;
      const c1 = [p1.x + (p2.x - p0.x) / 6, clamp(p1.y + (p2.y - p0.y) / 6)];
      const c2 = [p2.x - (p3.x - p1.x) / 6, clamp(p2.y - (p3.y - p1.y) / 6)];
      path += ` C ${round(c1[0])} ${round(c1[1])} ${round(c2[0])} ${round(c2[1])} ${round(p2.x)} ${round(p2.y)}`;
    }
    return path;
  }

  // Runs of consecutive observed hours from..to. An unobserved hour breaks the line; one point draws nothing.
  function runs(values, from, to) {
    const found = [[]];
    for (let hour = from; hour <= to; hour++) {
      if (values[hour] !== null) found[found.length - 1].push({ hour, value: values[hour] });
      else if (found[found.length - 1].length) found.push([]);
    }
    return found.filter(run => run.length > 1);
  }

  // The pace line in tokens per hour, only from a reported allowance. Percentages alone never make one.
  function pacePerHour(quotaWindow) {
    if (!quotaWindow || !Number.isFinite(quotaWindow.allowance_tokens) || !Number.isFinite(quotaWindow.pace_tokens_per_second)) return null;
    return quotaWindow.pace_tokens_per_second * 3600;
  }

  // "Last 24 hours 12k tokens · usual day 9k". A usual day is stated only when every hour has one.
  function totals(burn) {
    const live = `Last 24 hours ${short(sum(burn.last24h))} tokens`;
    return burn.avg.includes(null) ? live : `${live} · usual day ${short(sum(burn.avg))}`;
  }

  function svg(tag, attrs, text) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([key, value]) => item.setAttribute(key, String(value)));
    if (text !== undefined) item.textContent = text;
    return item;
  }

  /**
   * Draw one card's chart. x is the local clock hour; solid is the last 24 hours, split at the now
   * marker so the stretch after it (yesterday's tail) is faded; dashed is a usual day; the flat
   * wider-dash line is the pace, passed only for All bots. options: color, maximum, nowMs, label,
   * pace, paceTitle, usualTitle.
   */
  function drawBurn(burn, options) {
    const now = new Date(options.nowMs), nowHour = now.getHours();
    const band = { min: PAD_T, max: PAD_T + INNER_H };
    const x = hour => PAD_L + INNER_W * hour / 23;
    const y = value => band.max - INNER_H * value / options.maximum;
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, preserveAspectRatio: "none",
      role: "img", "aria-label": options.label });
    const vertical = (at, className) => svg("line", { x1: at, x2: at, y1: band.min, y2: band.max, class: className });
    LABELLED_HOURS.forEach(hour => chart.append(vertical(x(hour), "burn-hourline")));
    if (Number.isFinite(options.pace)) {
      const pace = svg("line", { x1: PAD_L, x2: W - PAD_R, y1: y(options.pace), y2: y(options.pace), class: "burn-pace" });
      pace.append(svg("title", {}, options.paceTitle));
      chart.append(pace);
    }
    const line = (run, className) => svg("path", { class: className, stroke: options.color,
      d: smoothPath(run.map(point => ({ x: x(point.hour), y: y(point.value) })), band) });
    runs(burn.avg, 0, 23).forEach(run => {
      const usual = line(run, "burn-usual");
      usual.append(svg("title", {}, options.usualTitle));
      chart.append(usual);
    });
    runs(burn.last24h, 0, nowHour).forEach(run => chart.append(line(run, "burn-line")));
    runs(burn.last24h, nowHour + 1, 23).forEach(run => chart.append(line(run, "burn-line burn-tail")));
    chart.append(vertical(x(Math.min(23, nowHour + now.getMinutes() / 60)), "burn-now"));
    // Hour labels are HTML, not SVG text, so a wide card stretches the plot without stretching the type.
    const frame = document.createElement("div");
    frame.className = "burn-chart";
    frame.append(chart);
    LABELLED_HOURS.forEach(hour => {
      const label = document.createElement("span");
      label.className = "burn-hour";
      label.style.left = `${x(hour) / W * 100}%`;
      label.textContent = hourLabel(hour);
      frame.append(label);
    });
    return frame;
  }

  function short(value) {
    if (value >= 1000000) return `${(value / 1000000).toFixed(1)}m`;
    if (value >= 1000) return `${Math.round(value / 1000)}k`;
    return String(Math.round(value));
  }

  return { hourLabel, hourlyBurn, sumBurns, ceiling, smoothPath, runs, pacePerHour, totals, drawBurn, short };
});
