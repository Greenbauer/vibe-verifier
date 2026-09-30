(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VVCharts = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function bucketSamples(samples, range, nowMs) {
    const count = range === "24h" ? 24 : 7;
    const width = range === "24h" ? 3600000 : 86400000;
    const end = nowMs;
    const start = end - count * width;
    const buckets = Array.from({ length: count }, (_, index) => ({
      start: start + index * width,
      end: start + (index + 1) * width,
      reviewer: null, explorer: null, verifier: null, total: null
    }));
    for (const sample of samples || []) {
      const time = Date.parse(sample.timestamp);
      if (!Number.isFinite(time) || time < start || time >= end) continue;
      const bucket = buckets[Math.min(count - 1, Math.floor((time - start) / width))];
      const amount = Number(sample.input_tokens) + Number(sample.output_tokens);
      if (!(sample.bot in bucket) || !Number.isFinite(amount)) continue;
      bucket[sample.bot] += amount;
      bucket.total += amount;
    }
    return buckets;
  }

  function seriesFor(samples, range, nowMs, bot) {
    return bucketSamples(samples, range, nowMs).map(bucket => ({ x: bucket.start, y: bucket[bot] }));
  }

  function linePath(points, x, y) {
    let connected = false;
    return points.map((point, index) => {
      if (!Number.isFinite(point.y)) { connected = false; return ""; }
      const segment = `${connected ? "L" : "M"}${x(index)},${y(point.y)}`;
      connected = true;
      return segment;
    }).filter(Boolean).join(" ");
  }

  function pacePerBucket(window, range) {
    if (!window || !Number.isFinite(window.allowance_tokens) || !Number.isFinite(window.pace_tokens_per_second)) return null;
    return window.pace_tokens_per_second * (range === "24h" ? 3600 : 86400);
  }

  function svg(tag, attrs, text) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([key, value]) => item.setAttribute(key, String(value)));
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function draw(container, points, options) {
    const width = Math.max(260, container.clientWidth || 500);
    const height = options.compact ? 64 : 84;
    const left = 38, right = 8, top = 12, bottom = 24;
    const pace = Number.isFinite(options.pace) ? options.pace : null;
    const maximum = Math.max(1, pace || 0, ...points.map(point => point.y));
    const x = index => left + index * (width - left - right) / Math.max(1, points.length - 1);
    const y = value => top + (height - top - bottom) * (1 - value / maximum);
    const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": options.label });
    chart.append(svg("line", { x1: left, x2: width - right, y1: y(0), y2: y(0), class: "chart-grid" }));
    chart.append(svg("line", { x1: left, x2: width - right, y1: y(maximum), y2: y(maximum), class: "chart-grid" }));
    chart.append(svg("text", { x: left - 6, y: y(maximum) + 4, class: "chart-label", "text-anchor": "end" }, short(maximum)));
    chart.append(svg("text", { x: left - 6, y: y(0) + 4, class: "chart-label", "text-anchor": "end" }, "0"));
    if (pace !== null) {
      const line = svg("line", { x1: left, x2: width - right, y1: y(pace), y2: y(pace), class: "chart-pace" });
      line.append(svg("title", {}, `Pace to reset: ${Math.round(pace).toLocaleString()} tokens per ${options.range === "24h" ? "hour" : "day"}`));
      chart.append(line);
    }
    if (points.length) {
      const path = linePath(points, x, y);
      chart.append(svg("path", { d: path, class: "chart-line", stroke: options.color }));
      points.forEach((point, index) => {
        if (Number.isFinite(point.y)) chart.append(svg("circle", { cx: x(index), cy: y(point.y), r: 2.5, fill: options.color }));
      });
      chart.append(svg("text", { x: left, y: height - 5, class: "chart-label" }, new Date(points[0].x).toLocaleDateString([], { month: "short", day: "numeric" })));
      chart.append(svg("text", { x: width - right, y: height - 5, class: "chart-label", "text-anchor": "end" }, "Now"));
    }
    container.replaceChildren(chart);
  }

  function short(value) {
    if (value >= 1000000) return `${(value / 1000000).toFixed(1)}m`;
    if (value >= 1000) return `${Math.round(value / 1000)}k`;
    return String(Math.round(value));
  }

  return { bucketSamples, seriesFor, linePath, pacePerBucket, draw, short };
});
