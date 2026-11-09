// A draft's not-run tests in the CI timeline (timeline.js), against the same minimal DOM as
// timeline.test.js. Run by tests/test_dashboard_timeline.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");

class Node {
  constructor(tag) {
    const styles = {};
    Object.assign(this, { tag, children: [], attrs: {}, className: "", textContent: "", handlers: {},
      style: { setProperty: (key, value) => { styles[key] = value; }, get: key => styles[key] } });
  }
  append(...items) { this.children.push(...items); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  get text() { return [this.textContent, ...this.children.map(child => child.text ?? String(child.data))].join(" ").replace(/\s+/g, " ").trim(); }
  find(className) {
    const found = [];
    const walk = node => { if (node.className?.split(" ").includes(className)) found.push(node); (node.children || []).forEach(walk); };
    walk(this);
    return found;
  }
}
global.Node = Node;
global.document = { createElement: tag => new Node(tag), createTextNode: data => ({ data, text: data }), querySelector: () => null };
global.CSS = { escape: value => value };
global.window = global;
global.VV = require("./helpers.js");
const timeline = require("./timeline.js");

const T0 = Date.parse("2026-10-19T14:00:00Z");
const at = (minutes, seconds = 0) => new Date(T0 + (minutes * 60 + seconds) * 1000).toISOString();
const NOW = T0 + 50 * 60 * 1000;
const job = (id, name, created, started, completed, extra = {}) => ({ id, name, created_at: created, started_at: started,
  completed_at: completed, html_url: `https://github.com/octocat/example/actions/runs/1/job/${id}`, steps: [], ...extra });
const check = (id, name, category, status = "completed") => ({ id, name, category, status });

const short = { repository: "octocat/example", number: 8, statuses: [], checks: [check(1, "lint", "success")],
  runs: [{ id: 30, name: "CI", jobs: [job(1, "lint", at(0), at(0, 10), at(10))] }] };

test("a draft's not-run tests are an empty outline across the lane, never red, never the critical path, and say why", () => {
  const draft = { repository: "octocat/example", number: 9, html_url: "https://github.com/octocat/example/pull/9", statuses: [],
    checks: [check(1, "Design tokens", "success"), check(2, "Lint and unit tests", "draft"), check(3, "All tests", "draft")],
    runs: [{ id: 1, name: "Code", jobs: [job(1, "Design tokens", at(0), at(0, 5), at(1)),
      job(2, "Lint and unit tests", at(0), at(0), at(0)), job(3, "All tests", at(1), at(1, 5), at(1, 7))] }] };
  const line = timeline.timeline(draft, NOW);
  assert.deepStrictEqual(line.lanes.filter(lane => lane.critical).map(lane => lane.name), ["Design tokens"]);
  const mini = timeline.mini(draft, NOW).find("timeline-bar");
  assert.deepStrictEqual(mini.map(bar => [bar.className, bar.style.get("left"), bar.style.get("width")]), [
    ["timeline-bar tl-draft", "0.000%", "100.000%"], ["timeline-bar tl-success", `${(5 / 67 * 100).toFixed(3)}%`, mini[1].style.get("width")]]);
  const box = timeline.detail(draft, NOW, { id: "x", key: "k", owner: "octocat", rerender() {} });
  assert.match(box.find("timeline-draft-note")[0].text, /have not run: this pull request is a draft\. They run when it is marked ready/);
  assert.strictEqual(box.find("timeline-draft-note")[0].find("evidence-link")[0].attrs.href, "https://github.com/octocat/example/pull/9");
  const rows = box.find("lane");
  assert.deepStrictEqual(rows.map(row => [row.find("timeline-name")[0].text, row.find("timeline-time")[0].text,
    row.find("timeline-bar").map(bar => bar.className).join(), row.className.includes("alt")]), [
    ["Lint and unit tests", "Not run (draft)", "timeline-bar tl-draft", false],
    ["Design tokens", "55s", "timeline-bar tl-success", true],
    ["All tests", "Not run (draft)", "timeline-bar tl-draft", false]]);
  assert.strictEqual(rows[0].find("timeline-bar")[0].attrs.title,
    "Lint and unit tests: not run, this pull request is a draft. It runs when marked ready for review.");
  assert.ok(box.find("timeline-legend")[0].text.includes("Not run (draft): runs when marked ready"));
  // A head with no draft lane shows neither the note nor the legend entry.
  const plain = timeline.detail(short, NOW, { id: "y", key: "s", owner: "octocat", rerender() {} });
  assert.deepStrictEqual([plain.find("timeline-draft-note").length, plain.find("timeline-legend")[0].text.includes("Not run")], [0, false]);
});
