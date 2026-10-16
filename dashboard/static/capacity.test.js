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
  return { name, bucket_seconds: step, buckets, covered_hours: 1, busy_percent: null, warm_hours: null, idle_hours: null, wait: null, ...extra };
}

function snapshot(history) {
  return { owner: "example", telemetry: { available: true, capacity: { available: true, stale: false, sampled_at: at(0),
    host: { cpu_percent: 10, memory_used_bytes: 1, memory_total_bytes: 2, workspace_disk_free_bytes: 1, workspace_disk_total_bytes: 2 },
    limits: { slots: 4, wait_slots: 2, qae_concurrency: 2, cpu_quota_cores: 4, memory_max_bytes: 1024 },
    listener_state: "up",
    history,
    lanes: [
      { id: "ci-1", state: "busy", registered: null, labels: ["ci"], allocated_at: at(12), started_at: at(11),
        job: job("<img src=x>", "https://github.com/example/widgets/actions/runs/9/job/8", 41) },
      { id: "wait-2", state: "busy", registered: null, labels: ["wait"], allocated_at: at(3), job: job("Wait for preview", null, null) },
      { id: "ci-3", state: "allocated", registered: null, labels: ["ci"], allocated_at: at(6) },
      { id: "ci-4", state: "allocated", registered: null, labels: ["ci"], allocated_at: at(2) },
      { id: "on-demand-1", state: "provisionable", registered: false, labels: [] },
      { id: "on-demand-2", state: "provisionable", registered: false, labels: [] }] } } };
}

function history() {
  const day = range("24h", 48, 1800, { busy_percent: 41.2, warm_hours: 20, idle_hours: 3.5,
    wait: { jobs: 120, p50_seconds: 18, p95_seconds: 130, routing_seconds: 3 } });
  day.buckets[47] = { start: day.buckets[47].start, running: 2, warm: 0.5, reserved: 0.5, waiting: 3 };
  day.buckets[46] = { start: day.buckets[46].start, running: 1, warm: 0, reserved: 0, waiting: 0 };
  return { observed_at: at(0), limit: 4, waiting_now: 0, warm: 1, warm_slots: ["ci-4", "ci-1"], started: {},
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
  assert.match(cards[0], /Runners in use now.*3 of 4.*Warm pool size: 1 \(the table marks which runners\)/s);
  assert.match(cards[1], /Jobs waiting for a runner.*0/s);
  assert.match(cards[2], /Busy share.*41\.2%.*Last 24 hours: share of the 4 runner slots running a job/s);
  assert.match(cards[3], /18s typical.*2m 10s for the slowest 5% of 120 jobs\. Before that, GitHub took 3s on average/s);
  assert.match(cards[4], /Idle runners.*3h 30m.*runners up with no job, not counting 20h 0m kept warm on purpose/s);
});

test("the limits read as a sentence", () => {
  assert.match(render(snapshot(history())).find("lane-limits")[0].text,
    /Up to 4 runners at once \(AI QA at most 2\) · 2 more for jobs that only wait · 4 CPU cores · 1 KB memory/);
});

test("each bar stacks running, kept warm and idle on the limit, marks waiting, and an empty bar says so", () => {
  const bars = render(snapshot(history())).find("capacity-bar");
  assert.strictEqual(bars.length, 48);
  const last = bars[47];
  assert.strictEqual(last.find("bar-running")[0].style.props.height, "50%");
  assert.strictEqual(last.find("bar-warm")[0].style.props.height, "12.5%");
  assert.strictEqual(last.find("bar-reserved")[0].style.props.height, "12.5%");
  assert.strictEqual(last.find("waiting-mark").length, 1);
  assert.match(last.attrs.title, /average 2 running, 0\.5 kept warm, 0\.5 idle, 1\.0 free; up to 3 jobs waiting/);
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

test("runners are grouped by kind in plain words, named by kind and slot, with free slots counted per pool", () => {
  const content = render(snapshot(history()));
  assert.deepStrictEqual(content.find("runner-pool").map(pool => pool.children[0].text.trim()),
    ["Runner slots: 3 of 4 in use", "Waiting-job slots: 1 of 2 in use"]);
  const titles = content.find("runner-group-title").map(title => title.text.replace(/\s+/g, " ").trim());
  assert.match(titles[0], /^Build and test Runs the repositories' CI jobs\./);
  assert.match(titles[1], /^AI QA Runs the QA explorer against a preview/);
  assert.match(titles[2], /^Waiting jobs Holds jobs that only wait/);
  assert.match(content.find("runner-group")[1].text, /None right now\..*At most 2 at once\./s);
  const rows = content.find("runner-row").slice(1).map(row => row.text.replace(/\s+/g, " ").trim());
  assert.deepStrictEqual(rows.length, 6);
  assert.match(rows[0], /^Build 1 Running <img src=x> widgets #41 11m/);
  // The listener names the runners its warm pool keeps (ci-4, and ci-1 which has since taken a job).
  assert.match(rows[1], /^Build 3 Idle, no job Waiting for GitHub to send a job 6m 0s idle/);
  assert.match(rows[2], /^Build 4 Kept warm Ready for the next job; kept up on purpose 2m 0s idle/);
  assert.match(rows[3], /^2 free A runner starts in a free slot when a job arrives\./);
  assert.match(rows[4], /^Wait 2 Running Wait for preview widgets 3m/);
  assert.match(rows[5], /^1 free Not counted in the runner slots above\./);
  const slot = content.find("runner-row")[1].children[0];
  assert.strictEqual(slot.attrs.title, "Service ci@1 on the runner machine");
  assert.deepStrictEqual(content.find("runner-head")[0].children.map(cell => cell.text.trim()), ["Runner", "State", "Job", "Repository", "Time"]);
});

test("only runners the listener names read kept warm, and only while they have no job", () => {
  const value = snapshot(history());
  value.telemetry.capacity.history.warm_slots = [];
  const rows = render(value).find("runner-row").slice(1).map(row => row.text.replace(/\s+/g, " ").trim());
  assert.match(rows[1], /^Build 3 Idle, no job/);
  assert.match(rows[2], /^Build 4 Idle, no job/);
  assert.doesNotMatch(rows.join(" "), /Kept warm/);
});

test("with the listener down, slots of unknown state are counted and explained, not dropped", () => {
  const value = snapshot(history());
  value.telemetry.capacity.lanes = value.telemetry.capacity.lanes.filter(lane => lane.id !== "on-demand-2").concat(
    { id: "on-demand-2", state: "unknown", registered: false, labels: [] }, { id: "on-demand-3", state: "unknown", registered: false, labels: [] });
  value.telemetry.capacity.listener_state = "down";
  const content = render(value);
  assert.match(content.find("capacity-card")[0].text, /Runners in use now.*3 of 4/s);
  const rows = content.find("runner-row").map(row => row.text.replace(/\s+/g, " ").trim());
  assert.ok(rows.some(row => /^2 unknown The runner listener is not answering/.test(row)));
  // The waiting-job pool's other slot is unknown too, not free.
  assert.ok(rows.some(row => /^1 unknown The runner listener is not answering/.test(row)));
  assert.ok(!rows.some(row => /^1 free Not counted/.test(row)));
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
  assert.strictEqual(content.find("runner-row").length, 7);
  assert.doesNotMatch(content.text, /Kept warm/);
  assert.match(content.find("capacity-card")[1].text, /Unknown/);
});

test("no capacity snapshot shows the empty state", () => {
  assert.match(render({ owner: "example", telemetry: { available: false } }).text, /Capacity telemetry unavailable/);
});
