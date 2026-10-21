// Renders the Work board (work.js) against a minimal DOM: the columns, where an item lands, the
// banners, and that every value reaches the page as text. Run by tests/test_dashboard_work.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");

class Node {
  constructor(tag) { Object.assign(this, { tag, children: [], attrs: {}, className: "", textContent: "", hidden: false, style: { setProperty() {} } }); }
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener() {}
  get text() { return [this.textContent, ...this.children.map(child => child.text ?? String(child.data))].join(" "); }
  find(className) {
    const found = [];
    const walk = node => { if (node.className?.split(" ").includes(className)) found.push(node); (node.children || []).forEach(walk); };
    walk(this);
    return found;
  }
}
global.Node = Node;
global.document = { createElement: tag => new Node(tag), createTextNode: data => ({ data, text: data }) };
global.window = global;
global.VV = require("./helpers.js");
require("./work.js");

const day = 24 * 60 * 60 * 1000;
const at = days => new Date(Date.now() - days * day).toISOString();
const item = (id, phase, status, extra = {}) => ({ id, label: id.toUpperCase(), title: `Title ${id}`, status, phase,
  created_at: at(30), updated_at: at(1), waiting: null, claimed_by: null, requester: null, pr_number: null, pr_url: null,
  preview_url: null, item_url: null, detail: false, body: null, updates: [], asks: [], refs: [], ...extra });

function render(work) {
  const content = new Node("main");
  const helpers = { heading: title => new Node("h1"), empty: (title, detail) => Object.assign(new Node("section"), { textContent: `${title} ${detail}` }),
    sourceBanner: text => Object.assign(new Node("div"), { className: "source-banner", textContent: text }) };
  window.VVWork.render(content, { work }, helpers);
  return content;
}

test("each item lands in one column, waiting items with their reason", () => {
  const content = render({ configured: true, sources: [{ id: "plans", label: "Plans", kind: "plans", items: [
    item("queued", "queued", "submitted", { updated_at: at(150) }),
    item("building", "active", "in_progress", { claimed_by: "dev@example.test" }),
    item("blocked", "active", "blocked", { waiting: { who: "requester", reason: "Blocked: waiting on a key" } }),
    item("merged", "done", "merged")] }] });
  const columns = Object.fromEntries(content.find("work-column").map(column => [column.attrs["aria-label"], column.text]));
  assert.deepStrictEqual(Object.keys(columns), ["To do", "In progress", "Waiting on you", "Done"]);
  // A column title is its name and count alone, with no sentence describing the column.
  assert.deepStrictEqual(content.find("work-column-title").map(title => title.text.trim()),
    ["To do (1)", "In progress (1)", "Waiting on you (1)", "Done (1)"]);
  assert.match(columns["To do"], /QUEUED/);
  assert.match(columns["In progress"], /BUILDING.*claimed by dev@example\.test/s);
  assert.match(columns["Waiting on you"], /BLOCKED.*Blocked: waiting on a key/s);
  assert.doesNotMatch(columns["In progress"], /BLOCKED/);
  assert.match(columns.Done, /MERGED/);
  assert.match(content.find("work-summary")[0].text, /1 to do · 1 in progress · 1 waiting on you · 1 open items not updated/);
  // The stale card's update age carries the red class.
  assert.ok(content.find("age-red").length === 1);
});

test("a source that cannot be read says why instead of drawing an empty board as fine", () => {
  const content = render({ configured: true, sources: [{ id: "calls", label: "Calls", kind: "calls", items: [], error: "credential_missing", available: false }] });
  assert.match(content.find("source-banner")[0].text, /Calls\. No credential file on this host yet\./);
});

test("no sources shows the empty state", () => {
  assert.match(render({ configured: false, sources: [] }).text, /No work sources/);
});

test("markup in a title or body stays text", () => {
  const content = render({ configured: true, sources: [{ id: "p", label: "P", kind: "plans", items: [
    item("x", "queued", "submitted", { title: "<img src=x onerror=alert(1)>", detail: true, body: "<script>alert(2)</script>" })] }] });
  const tags = [];
  const walk = node => { if (node.tag) tags.push(node.tag); (node.children || []).forEach(walk); };
  walk(content);
  assert.ok(!tags.includes("img") && !tags.includes("script"));
  assert.match(content.text, /<img src=x onerror=alert\(1\)>/);
  assert.match(content.text, /<script>alert\(2\)<\/script>/);
});
