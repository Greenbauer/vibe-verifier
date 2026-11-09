// The CI timeline's axis (timeline-axis.js): positions clamp to the span, and the ticks are round
// times labeled in seconds, minutes and hours. Run by tests/test_dashboard_timeline.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");

global.document = { createElement: tag => ({ tag, children: [], attrs: {}, className: "", textContent: "",
  style: { setProperty() {} }, setAttribute(key, value) { this.attrs[key] = value; }, append(...items) { this.children.push(...items); } }),
  createTextNode: data => ({ data }) };
global.Node = Object;
global.VV = require("./helpers.js");
const { percent, ticks, axis } = require("./timeline-axis.js");

test("a position is a share of the span, clamped to the track", () => {
  assert.deepStrictEqual([percent(30, 120), percent(-5, 120), percent(500, 120)], ["25.000%", "0.000%", "100.000%"]);
});

test("ticks are round times, at most six, labeled 0, then seconds, minutes or hours", () => {
  assert.deepStrictEqual(ticks(2 * 3600 * 1000), [0, 1800, 3600, 5400, 7200]);
  const labels = axis(2 * 3600 * 1000).children[1].children.map(tick => tick.children.map(child => child.data).join(""));
  assert.deepStrictEqual(labels, ["0", "30m", "1h", "1h 30m", "2h"]);
});
