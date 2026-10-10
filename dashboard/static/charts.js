(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VVCharts = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  // One hour-of-day chart in viewBox units. The SVG stretches to its card's width.
  const W = 280, H = 126, PAD_L = 4, PAD_R = 4, PAD_T = 8, PAD_B = 16;
  const INNER_W = W - PAD_L - PAD_R, INNER_H = H - PAD_T - PAD_B;
  const DAY = 86400000, HOUR = 3600000, PRIOR_DAYS = 6, LABELLED_HOURS = [0, 6, 12, 18];

  const sum = values => values.reduce((total, value) => total + (value || 0), 0);

  // "12a" / "6a" / "12p" / "6p": a compact clock-hour label.
  function hourLabel(hour) {
    return `${hour % 12 === 0 ? 12 : hour % 12}${hour < 12 ? "a" : "p"}`;
  }

  /**
   * Tokens per hour slot, where slot k is the k-th local clock hour before the current one (slot 0).
   * Slots come from the local calendar date and clock hour, not elapsed time, so a daylight-saving
   * change never moves a sample into a neighbouring hour or day. The first sample counts even when it
   * is older than the oldest slot: it still proves the bot was observed from then on.
   */
  function slotTotals(samples, now) {
    const midnight = time => new Date(time).setHours(0, 0, 0, 0);
    const slotOf = time => Math.round((midnight(now) - midnight(time)) / DAY) * 24 + now.getHours() - new Date(time).getHours();
    const cells = new Array((PRIOR_DAYS + 1) * 24).fill(0);
    let first = Infinity, last = -Infinity;
    for (const sample of samples || []) {
      const time = Date.parse(sample.timestamp);
      const amount = Number(sample.input_tokens) + Number(sample.output_tokens);
      if (!Number.isFinite(time) || !Number.isFinite(amount) || time > now.getTime()) continue;
      first = Math.min(first, time);
      last = Math.max(last, time);
      if (slotOf(time) < cells.length) cells[slotOf(time)] += amount;
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
    const now = new Date(nowMs);
    const { cells, slotOf, first, last } = slotTotals(samples, now);
    if (first === Infinity) return null;
    const through = Math.max(last, Number.isFinite(observedThroughMs) ? Math.min(nowMs, observedThroughMs) : nowMs);
    const oldest = Math.min(slotOf(first), cells.length - 1), newest = slotOf(through);
    const value = slot => (slot >= newest && slot <= oldest ? cells[slot] : null);
    const last24h = new Array(24).fill(null), avg = new Array(24).fill(null), days = [];
    for (let slot = 0; slot < 24; slot++) {
      const hour = (((now.getHours() - slot) % 24) + 24) % 24;
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
  function ceiling(burns) {
    const values = burns.flatMap(burn => [...burn.last24h, ...burn.avg]).filter(Number.isFinite);
    return Math.max(1, ...values);
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

  // Runs of consecutive observed hours from..to. An unobserved hour breaks the line.
  function runs(values, from, to) {
    const found = [[]];
    for (let hour = from; hour <= to; hour++) {
      if (values[hour] !== null) found[found.length - 1].push({ hour, value: values[hour] });
      else if (found[found.length - 1].length) found.push([]);
    }
    return found.filter(run => run.length);
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

  // The chart in its frame. Axis labels are HTML, not SVG text, so a wide card stretches the plot
  // without stretching the type. labels: [{ x, text }], x in viewBox units.
  function framed(chart, labels) {
    const frame = document.createElement("div");
    frame.className = "burn-chart";
    frame.append(chart);
    labels.forEach(({ x, text }) => {
      const label = document.createElement("span");
      label.className = "burn-hour";
      label.style.left = `${x / W * 100}%`;
      label.textContent = text;
      frame.append(label);
    });
    return frame;
  }

  /**
   * Draw one card's chart. x is the local clock hour; solid is the last 24 hours, split at the now
   * marker so the stretch after it (yesterday's tail) is faded; dashed is a usual day.
   * options: color, maximum, nowMs, label, usualTitle.
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
    const nowX = x(Math.min(23, nowHour + now.getMinutes() / 60));
    const plot = run => run.map(point => ({ x: x(point.hour), y: y(point.value) }));
    const line = (points, className, title) => {
      if (points.length < 2) return;  // a lone point would draw nothing; no dots
      const path = svg("path", { class: className, stroke: options.color, d: smoothPath(points, band) });
      if (title) path.append(svg("title", {}, title));
      chart.append(path);
    };
    runs(burn.avg, 0, 23).forEach(run => line(plot(run), "burn-usual", options.usualTitle));
    // Today's line runs on to the now marker, so the current hour shows even when it is the only one.
    runs(burn.last24h, 0, nowHour).forEach(run => {
      const points = plot(run);
      if (run[run.length - 1].hour === nowHour) points.push({ x: nowX, y: points[points.length - 1].y });
      line(points, "burn-line");
    });
    runs(burn.last24h, nowHour + 1, 23).forEach(run => line(plot(run), "burn-line burn-tail"));
    chart.append(vertical(nowX, "burn-now"));
    return framed(chart, LABELLED_HOURS.map(hour => ({ x: x(hour), text: hourLabel(hour) })));
  }

  // Gridlines across a plan window: local midnights for a window of two days or more, named by
  // weekday (by date past eight days), and clock hours for a shorter one. Stepped so there are at
  // most eight. x is the share of the window elapsed.
  function windowTicks(start, reset) {
    const span = reset - start, byDay = span >= 2 * DAY, step = Math.ceil(span / (byDay ? DAY : HOUR) / 8);
    const dated = span > 8 * DAY ? { month: "short", day: "numeric" } : { weekday: "short" };
    const at = new Date(start), ticks = [];
    if (byDay) at.setHours(24, 0, 0, 0);
    else at.setMinutes(60, 0, 0);
    while (at.getTime() < reset) {
      ticks.push({ x: (at.getTime() - start) / span, label: byDay ? at.toLocaleDateString([], dated) : hourLabel(at.getHours()) });
      if (byDay) at.setDate(at.getDate() + step);
      else at.setHours(at.getHours() + step);
    }
    return ticks;
  }

  /**
   * One plan window through time, for the subscription burn chart. x is the share of the window
   * elapsed (0 at its start, 1 at its reset) and y is percent used, so the even pace is the straight
   * line from 0% to 100%: what the tick on the usage bar marks, and `pointsPerHour` in the plan's own
   * unit. Percent used is drawn, not points per hour: readings are whole percents, and a week-long
   * plan gains one every hour and forty minutes at the even pace, so an hourly rate is a string of
   * zeros and ones until it is smoothed over half a day. `runs` are the hourly readings, split
   * wherever a clock hour has none, so a gap is never drawn across. Null when the window keeps no
   * history (no collector reads it) or reports no length.
   */
  function planBurn(window, nowMs) {
    const span = window.window_minutes * 60000, reset = Date.parse(window.resets_at), start = reset - span;
    if (!Array.isArray(window.history) || !(span > 0) || !Number.isFinite(reset)) return null;
    const share = time => Math.min(1, Math.max(0, (time - start) / span)), hour = time => Math.floor(time / HOUR);
    const readings = window.history.map(point => ({ time: Date.parse(point.at), used: point.used_percent }));
    const runs = [];
    readings.forEach((reading, index) => {
      if (index === 0 || hour(reading.time) - hour(readings[index - 1].time) > 1) runs.push([]);
      runs[runs.length - 1].push({ x: share(reading.time), y: reading.used });
    });
    return { runs, count: readings.length, since: readings.length ? readings[0].time : null, now: share(nowMs),
      pointsPerHour: 100 * HOUR / span, ticks: windowTicks(start, reset) };
  }

  // "0.6" for a week-long window, "20" for a five-hour one.
  function pacePoints(burn) {
    return String(Number(burn.pointsPerHour.toPrecision(2)));
  }

  // "Even pace 0.6 points an hour · 28 hourly readings since Oct 7, 6:11 PM". sinceText is burn.since, formatted.
  function planTotals(burn, sinceText) {
    return `Even pace ${pacePoints(burn)} points an hour · ${burn.count} hourly reading${burn.count === 1 ? "" : "s"} since ${sinceText}`;
  }

  /**
   * Draw one plan window (see planBurn): a faint line at each tick and at half used, the even pace as
   * the wider-dash diagonal, each run of readings as a solid line (a reading with no neighbour is a
   * dot), and the now marker. options: color, label.
   */
  function drawPlan(burn, options) {
    const top = PAD_T, bottom = PAD_T + INNER_H, round = n => Math.round(n * 10) / 10;
    const x = share => PAD_L + INNER_W * share, y = percent => bottom - INNER_H * percent / 100;
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, preserveAspectRatio: "none",
      role: "img", "aria-label": options.label });
    const vertical = (share, className) => svg("line", { x1: x(share), x2: x(share), y1: top, y2: bottom, class: className });
    burn.ticks.forEach(tick => chart.append(vertical(tick.x, "burn-hourline")));
    chart.append(svg("line", { x1: x(0), x2: x(1), y1: y(50), y2: y(50), class: "burn-hourline" }));
    const pace = svg("line", { x1: x(0), x2: x(1), y1: y(0), y2: y(100), class: "burn-pace" });
    pace.append(svg("title", {}, `Even pace: ${pacePoints(burn)} points an hour, 100% over the window, the same as the tick.`));
    chart.append(pace);
    burn.runs.forEach(run => {
      const d = run.map((point, index) => `${index ? "L" : "M"} ${round(x(point.x))} ${round(y(point.y))}`).join(" ");
      chart.append(svg("path", run.length > 1 ? { class: "burn-line", stroke: options.color, d }
        : { class: "burn-line burn-dot", stroke: options.color, d: `${d} h 0` }));
    });
    chart.append(vertical(burn.now, "burn-now"));
    const frame = framed(chart, burn.ticks.map(tick => ({ x: x(tick.x), text: tick.label })));
    [100, 50].forEach(level => {
      const label = document.createElement("span");
      label.className = "burn-level";
      label.style.top = `${y(level) / H * 100}%`;
      label.textContent = `${level}%`;
      frame.append(label);
    });
    return frame;
  }

  function short(value) {
    if (value >= 1000000) return `${(value / 1000000).toFixed(1)}m`;
    if (value >= 1000) return `${Math.round(value / 1000)}k`;
    return String(Math.round(value));
  }

  return { hourLabel, hourlyBurn, sumBurns, ceiling, smoothPath, runs, totals, drawBurn, short,
    windowTicks, planBurn, planTotals, drawPlan };
});
