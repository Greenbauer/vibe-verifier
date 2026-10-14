// Renders the Capacity view (capacity.js) against a minimal DOM: the summary, the bars, one row per
// runner with free ones folded, the range buttons, and that every value reaches the page as text.
// Run by tests/test_dashboard_server_ui.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");

class Node {
  constructor(tag) { Object.assign(this, { tag, children: [], attrs: {}, className: "", textContent: "", listeners: {}, style: { props: {}, setProperty(key, value) { this.props[key] = value; } } }); }
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
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
const capacityView = require("./capacity.js");

const minute = 60 * 1000;
const at = minutes => new Date(Date.now() - minutes * minute).toISOString();
const job = (name, url, pull) => ({ repository: "example/widgets", name, url, pull });

function range(name, bars, step, extra = {}) {
  const buckets = Array.from({ length: bars }, (_, index) => ({ start: at((bars - index) * step / 60) }));
  return { name, bucket_seconds: step, buckets, covered_hours: 1, busy_percent: null, reserved_hours: null, wait: null, ...extra };
}

function snapshot(history) {
  return { owner: "example", telemetry: { available: true, capacity: { available: true, stale: false, sampled_at: at(0),
    host: { cpu_percent: 10, memory_used_bytes: 1, memory_total_bytes: 2, workspace_disk_free_bytes: 1, workspace_disk_total_bytes: 2 },
    limits: { slots: 4, wait_slots: 2, qae_concurrency: 2, cpu_quota_cores: 4, memory_max_bytes: 1024 },
    history,
    lanes: [
      { id: "ci-1", state: "busy", registered: null, labels: ["ci"], allocated_at: at(12), started_at: at(11),
        job: job("<img src=x>", "https://github.com/example/widgets/actions/runs/9/job/8", 41) },
      { id: "wait-2", state: "busy", registered: null, labels: ["wait"], allocated_at: at(3), job: job("Wait for preview", null, null) },
      { id: "ci-3", state: "allocated", registered: null, labels: ["ci"], allocated_at: at(6) },
      { id: "on-demand-1", state: "provisionable", registered: false, labels: [] },
      { id: "on-demand-2", state: "provisionable", registered: false, labels: [] }] } } };
}

function history() {
  const day = range("24h", 48, 1800, { busy_percent: 41.2, reserved_hours: 3.5,
    wait: { jobs: 120, p50_seconds: 18, p95_seconds: 130, routing_seconds: 3 } });
  day.buckets[47] = { start: day.buckets[47].start, running: 2, reserved: 1, waiting: 3 };
  day.buckets[46] = { start: day.buckets[46].start, running: 1, reserved: 0, waiting: 0 };
  return { observed_at: at(0), limit: 4, waiting_now: 0, warm: 1, started: {},
    ranges: [range("1h", 30, 120), day, range("7d", 56, 10800)] };
}

function render(value, rerender = () => {}) {
  const content = new Node("main");
  const helpers = { heading: title => Object.assign(new Node("h1"), { textContent: title }),
    empty: (title, detail) => Object.assign(new Node("section"), { textContent: `${title} ${detail}` }),
    sourceBanner: text => Object.assign(new Node("div"), { className: "source-banner", textContent: text }), rerender };
  capacityView.render(content, value, helpers);
  return content;
}

test("the summary says what is in use, what waits, and the chosen range's share and waits", () => {
  const cards = render(snapshot(history())).find("capacity-card").map(card => card.text);
  assert.match(cards[0], /Runners in use now.*2 of 4.*1 kept warm on purpose/s);
  assert.match(cards[1], /Jobs waiting for a runner.*0/s);
  assert.match(cards[2], /Busy share.*41\.2%.*Last 24 hours/s);
  assert.match(cards[3], /18s typical.*2m 10s for the slowest 5% of 120 jobs\. Before that, GitHub took 3s on average/s);
  assert.match(cards[4], /Reserved but idle.*3h 30m/s);
});

test("the limits read as a sentence", () => {
  assert.match(render(snapshot(history())).find("lane-limits")[0].text,
    /Up to 4 runners at once \(QAE at most 2\) · 2 more for jobs that only wait · 4 CPU cores · 1 KB memory/);
});

test("each bar stacks running and reserved on the limit, marks waiting, and an empty bar says so", () => {
  const bars = render(snapshot(history())).find("capacity-bar");
  assert.strictEqual(bars.length, 48);
  const last = bars[47];
  assert.strictEqual(last.find("bar-running")[0].style.props.height, "50%");
  assert.strictEqual(last.find("bar-reserved")[0].style.props.height, "25%");
  assert.strictEqual(last.find("waiting-mark").length, 1);
  assert.match(last.attrs.title, /average 2 running, 1 reserved, 1\.0 free; up to 3 jobs waiting/);
  assert.strictEqual(bars[46].find("waiting-mark").length, 0);
  assert.ok(bars[0].className.includes("no-data"));
  assert.match(bars[0].attrs.title, /no samples/);
});

test("a range button redraws in that range", () => {
  let redrawn = 0;
  const content = render(snapshot(history()), () => { redrawn += 1; });
  const buttons = content.find("range-buttons")[0].children;
  assert.deepStrictEqual(buttons.map(button => [button.text.trim(), button.attrs["aria-pressed"]]),
    [["1h", "false"], ["24h", "true"], ["7d", "false"]]);
  buttons[2].listeners.click();
  assert.strictEqual(redrawn, 1);
  assert.strictEqual(render(snapshot(history())).find("capacity-bar").length, 56);
  render(snapshot(history())).find("range-buttons")[0].children[1].listeners.click();
});

test("each runner is a row named as its unit, with free runners folded into one row", () => {
  const rows = render(snapshot(history())).find("runner-row").slice(1).map(row => row.text.replace(/\s+/g, " ").trim());
  assert.strictEqual(rows.length, 4);
  assert.match(rows[0], /^ci@1 Running <img src=x> widgets #41 CI 11m/);
  assert.match(rows[1], /^wait@2 Running Wait for preview widgets Wait 3m/);
  assert.match(rows[2], /^ci@3 Reserved, no job Waiting for GitHub to send a job CI 6m 0s idle/);
  assert.match(rows[3], /^2 more Free A runner starts when a job arrives/);
});

test("markup in a job name stays text", () => {
  const tags = [];
  const walk = node => { if (node.tag) tags.push(node.tag); (node.children || []).forEach(walk); };
  walk(render(snapshot(history())));
  assert.ok(!tags.includes("img"));
});

test("without history the page still shows the runners and says the history is coming", () => {
  const content = render(snapshot(null));
  assert.match(content.text, /No history yet/);
  assert.strictEqual(content.find("runner-row").length, 5);
  assert.match(content.find("capacity-card")[1].text, /Unknown/);
});

test("no capacity snapshot shows the empty state", () => {
  assert.match(render({ owner: "example", telemetry: { available: false } }).text, /Capacity telemetry unavailable/);
});
