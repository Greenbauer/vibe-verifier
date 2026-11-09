// quota.js on its own, and helpers.js handing on every name it exports, so the Usage view keeps
// reading them from VV. The window math itself: tests/test_dashboard_usage_quota.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const quota = require("./quota.js");
const helpers = require("./helpers.js");

test("helpers.js re-exports every quota function unchanged", () => {
  assert.ok(Object.keys(quota).length >= 9);
  Object.entries(quota).forEach(([name, value]) => assert.strictEqual(helpers[name], value, name));
});

test("quota.js works without helpers.js", () => {
  assert.deepStrictEqual([quota.quotaWindowLabel({ window_minutes: 300 }), quota.quotaWindowLabel({ window_minutes: 10080 })],
    ["5 hours", "7 days"]);
});
