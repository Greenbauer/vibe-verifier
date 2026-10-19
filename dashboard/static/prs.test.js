// Renders the Pull requests view (prs.js) against a minimal DOM: rows grouped by repository, a row's
// timeline in the Checks column with its words in the CI column, a meter row across both, and a click
// opening the timeline under its row. Run by tests/test_dashboard_timeline.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");

let focused = null;
class Node {
  constructor(tag) {
    const styles = {};
    Object.assign(this, { tag, children: [], attrs: {}, className: "", textContent: "", handlers: {},
      style: { setProperty: (key, value) => { styles[key] = value; } } });
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  focus() { focused = this; }
  get text() { return [this.textContent, ...this.children.map(child => child.text ?? String(child.data))].join(" ").replace(/\s+/g, " ").trim(); }
  walk() { return [this, ...this.children.filter(child => child instanceof Node).flatMap(child => child.walk())]; }
  find(className) { return this.walk().filter(node => node.className.split(" ").includes(className)); }
}
const content = new Node("main");
const select = selector => {
  const id = /^#(.+)$/.exec(selector)?.[1];
  const key = /^\[data-focus-key="(.+)"\]$/.exec(selector)?.[1];
  return content.walk().find(node => (id && node.attrs.id === id) || (key && node.attrs["data-focus-key"] === key)) || null;
};
global.Node = Node;
global.document = { createElement: tag => new Node(tag), createTextNode: data => ({ data, text: data }), querySelector: select };
global.CSS = { escape: value => value };
global.window = global;
global.VV = require("./helpers.js");
global.VVTimeline = require("./timeline.js");
const prs = require("./prs.js");

const at = minutes => new Date(Date.now() - (60 - minutes) * 60 * 1000).toISOString();
const pull = (number, extra) => ({ repository: "octocat/example", number, title: `Change ${number}`, author: "octocat",
  head_sha: "abcdef1234567890", html_url: `https://github.com/octocat/example/pull/${number}`, created_at: at(0),
  statuses: [], expected: [], runs: [], checks: [], ...extra });
const timed = pull(2, { checks: [{ id: 1, name: "test", category: "failed", status: "completed" }],
  runs: [{ id: 9, name: "CI", jobs: [{ id: 1, name: "test", created_at: at(0), started_at: at(1), completed_at: at(20), steps: [] }] }] });
const untimed = pull(1, { checks: [{ id: 3, name: "lint", category: "pending", status: "queued" }], behind_by: 2 });
const snapshot = { owner: "octocat", github: { coverage: { selected: 1 }, sampled_at: at(59),
  repositories: [{ repository: "octocat/example", pulls: [timed, untimed] }] } };
const state = { query: "", repository: "all", subscribed: false, attention: false, timeScale: "shared" };
const page = { heading: (title, warning, controls) => VV.element("div", { class: "heading" }, title, controls),
  empty: title => VV.element("p", {}, title), rememberView() {} };
const rows = () => content.find("pr-list")[0].children;
const row = number => rows().find(node => node.className.startsWith("pr-row") && node.text.includes(`Change ${number}`));

test("a row with a timeline puts the bars in Checks and the words in CI; a meter row keeps both in one cell", () => {
  prs.render(content, snapshot, state, page);
  assert.deepStrictEqual(content.find("list-heading")[0].children.map(cell => cell.text), ["2 pull requests", "Current work", "Checks", "CI"]);
  const cells = row(2).children.map(cell => cell.className);
  assert.deepStrictEqual(cells.slice(2, 4), ["timeline-cell", "progress-copy ci-copy"]);
  assert.match(row(2).find("ci-copy")[0].text, /1 failed/);
  assert.strictEqual(row(2).find("timeline-mini").length, 1);
  assert.deepStrictEqual(row(1).children.map(cell => cell.className).slice(2, 3), ["progress-copy"]);
  assert.strictEqual(row(1).find("check-meter").length, 1);
});

test("the filters come as the search, then the two menus together, then the two checkboxes together", () => {
  prs.render(content, snapshot, state, page);
  const toolbar = content.find("toolbar")[0];
  assert.deepStrictEqual(toolbar.children.map(node => [node.tag, node.className]),
    [["input", ""], ["div", "toolbar-group"], ["div", "toolbar-group"]]);
  const [, menus, checks] = toolbar.children;
  assert.deepStrictEqual(menus.children.map(node => node.attrs.id), ["repo-filter", "time-scale"]);
  assert.strictEqual(menus.children[0].children[0].text, "All repositories");
  assert.deepStrictEqual(checks.children.map(node => node.text), ["Subscribed only", "Needs attention"]);
});

test("a click on a row opens its timeline under it and keeps focus on its arrow; the arrow closes it", () => {
  prs.render(content, snapshot, state, page);
  row(2).handlers.click({ target: { closest: () => null } });
  assert.deepStrictEqual(rows().map(node => node.className), ["pr-row open", "timeline-detail", "pr-row"]);
  const arrow = row(2).walk().find(node => node.tag === "button");
  assert.deepStrictEqual([arrow.attrs["aria-expanded"], focused === arrow], ["true", true]);
  assert.strictEqual(rows()[1].attrs.id, arrow.attrs["aria-controls"]);
  arrow.handlers.click();
  assert.deepStrictEqual(rows().map(node => node.className), ["pr-row", "pr-row"]);
});

test("the first column reads title, then number, author and head, then the age and how far behind beside it", () => {
  prs.render(content, snapshot, state, page);
  const identity = row(1).find("pr-identity")[0];
  assert.deepStrictEqual(identity.children.map(node => node.tag), ["b", "small", "span"]);
  assert.strictEqual(identity.children[1].text, "#1 · octocat · abcdef12");
  const bottom = identity.children[2];
  assert.deepStrictEqual([bottom.className, bottom.children.map(node => node.className)], ["pr-meta", ["pr-age", "behind-mark"]]);
  assert.match(bottom.children[0].text, /^1h 0m old$/);
  assert.strictEqual(bottom.children[1].text, "2 behind main");
});

test("opening one row's timeline closes any other", () => {
  prs.render(content, snapshot, state, page);
  row(2).handlers.click({ target: { closest: () => null } });
  row(1).handlers.click({ target: { closest: () => null } });
  assert.deepStrictEqual(rows().map(node => node.className), ["pr-row", "pr-row open", "timeline-detail"]);
  assert.strictEqual(content.find("timeline-detail").length, 1);
  row(1).handlers.click({ target: { closest: () => null } });
  assert.deepStrictEqual(rows().map(node => node.className), ["pr-row", "pr-row"]);
});
