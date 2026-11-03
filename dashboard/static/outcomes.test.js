// Renders the Outcomes view (outcomes.js) against a minimal DOM: each merged pull request's time split
// with the CI timeline's rules, the week's medians against the week before, runner time per check, and
// the view in each state of GET /api/outcomes. Run by tests/test_dashboard_outcomes.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert");

class Node {
  constructor(tag) { Object.assign(this, { tag, children: [], attrs: {}, className: "", textContent: "", listeners: {}, style: { setProperty() {} } }); }
  append(...items) { this.children.push(...items.filter(item => item != null)); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  get text() { return [this.textContent, ...this.children.map(child => child.text ?? String(child.data))].join(" ").replace(/\s+/g, " ").trim(); }
  find(className) {
    const found = [];
    const walk = node => { if (node.className?.split(" ").includes(className)) found.push(node); (node.children || []).forEach(walk); };
    walk(this);
    return found;
  }
}
global.Node = Node;
global.document = { createElement: tag => new Node(tag), createElementNS: (_, tag) => new Node(tag),
  createTextNode: data => ({ data, text: data }), querySelector: () => null };
global.CSS = { escape: value => value };
global.window = global;
global.VV = require("./helpers.js");
const outcomes = require("./outcomes.js");
const trend = require("./outcome-trend.js");
const { element: el } = global.VV;

const HOUR = 3600 * 1000;
const T0 = Date.now() - 30 * HOUR;
const at = (minutes, seconds = 0) => new Date(T0 + (minutes * 60 + seconds) * 1000).toISOString();
const job = (id, name, created, started, completed) => ({ id, name, status: "completed", conclusion: "success",
  category: "success", created_at: created, started_at: started, completed_at: completed });

// Build ran with GitHub's normal 10-second pickup; QA, created when build finished, waited 10 minutes
// for a runner and ran 20; lint waited 5 minutes alongside but finished long before, so it set nothing.
function record(number, { merged = at(120), ready = at(30), runs, checks } = {}) {
  return { repository: "octocat/example", number, title: `Change ${number}`, url: `https://github.com/octocat/example/pull/${number}`,
    created_at: at(-60), merged_at: merged, ready_at: ready,
    head_runs: runs ?? [
      { id: 1, name: "CI", jobs: [job(11, "build", at(0), at(0, 10), at(10, 10)), job(12, "lint", at(0), at(5), at(6))] },
      { id: 2, name: "QA", jobs: [job(21, "explore", at(10, 12), at(20, 12), at(40, 12))] }],
    checks: checks ?? [{ name: "CI / build", jobs: 2, seconds: 1200, self_hosted_seconds: 1200 },
      { name: "QA / explore", jobs: 1, seconds: 1200, self_hosted_seconds: 0 }] };
}

test("a pull request's round splits along its critical chain and the merge wait starts once checks and draft are both done", () => {
  const split = outcomes.outcome(record(1));
  assert.strictEqual(split.round.wait_seconds, 600);
  assert.strictEqual(split.round.running_seconds, 40 * 60 + 12 - 600);
  // Ready for review at 0:30, before the checks finished at 0:40:12: merge-ready is the later of the two.
  assert.strictEqual(split.merge_wait_seconds, (120 - 40) * 60 - 12);
  // Left draft after the checks finished: merge-ready is the ready time.
  assert.strictEqual(outcomes.outcome(record(2, { ready: at(60) })).merge_wait_seconds, 60 * 60);
  const docs = outcomes.outcome(record(3, { runs: [] }));
  assert.strictEqual(docs.round, null);
  assert.strictEqual(docs.merge_wait_seconds, 90 * 60);
});

test("an earlier round on the merged head is not this round", () => {
  const rerun = record(4, { runs: [{ id: 1, name: "CI", jobs: [job(11, "build", at(-200), at(-190), at(-180)),
    job(13, "build", at(0), at(0, 5), at(5))] }] });
  assert.deepStrictEqual(outcomes.outcome(rerun).round && [outcomes.outcome(rerun).round.wait_seconds, outcomes.outcome(rerun).round.running_seconds], [0, 300]);
});

test("medians and runner time per check", () => {
  assert.strictEqual(outcomes.median([3, 1, 2]), 2);
  assert.strictEqual(outcomes.median([4, 1, 2, 3]), 2.5);
  assert.strictEqual(outcomes.median([null, undefined]), null);
  const rows = [record(1), record(2, { ready: at(60) })].map(outcomes.outcome);
  assert.deepStrictEqual(outcomes.measures(rows), { count: 2, wait: 600, running: 1812, merge: (4788 + 3600) / 2 });
  const other = outcomes.outcome(record(5, { checks: [{ name: "Lint / ruff", jobs: 1, seconds: 3000, self_hosted_seconds: 3000 }] }));
  assert.deepStrictEqual(outcomes.checkRows([...rows, other]).map(row => [row.name, row.jobs, row.seconds, row.self_hosted_seconds]),
    [["Lint / ruff", 1, 3000, 3000], ["CI / build", 4, 2400, 2400], ["QA / explore", 2, 2400, 0]]);
});

const helpers = rerender => ({
  heading: title => el("h1", {}, title),
  empty: (title, detail) => el("section", { class: "empty" }, title, detail),
  sourceBanner: text => el("div", { class: "source-banner" }, text),
  rerender,
});

function serve(body, ok = true) {
  global.fetch = async url => { assert.strictEqual(url, "/api/outcomes"); return { ok, json: async () => body }; };
}

async function rendered(body, ok = true) {
  Object.assign(outcomes.source, { view: null, fetched: 0, loading: false, failed: false });
  serve(body, ok);
  const content = new Node("main");
  let done;
  const again = new Promise(resolve => { done = resolve; });
  outcomes.render(content, { owner: "octocat" }, helpers(done));
  const first = content.text;
  await again;
  outcomes.render(content, { owner: "octocat" }, helpers(() => {}));
  return { first, content };
}

test("the view loads from /api/outcomes and shows the week against the week before", async () => {
  const older = record(9, { merged: new Date(Date.now() - 10 * 24 * HOUR).toISOString(), runs: [] });
  const { first, content } = await rendered({ available: true, window_days: 14, sampled_at: at(130), pending: 0, error: null,
    pulls: [record(1), record(2), older] });
  assert.match(first, /Loading merged pull requests/);
  const cards = content.find("capacity-card").map(card => card.text);
  assert.match(cards[0], /Merged pull requests 2 Previous 7 days: 1/);
  assert.match(cards[1], /Waiting for a runner \(median\) 10m 0s Previous 7 days: None/);
  assert.match(cards[3], /Waiting on the merge \(median\) 1h 19m Previous 7 days: 0s/);
  const pulls = content.find("outcome-pulls").slice(1).map(row => row.text);
  assert.strictEqual(pulls.length, 2);
  assert.match(pulls[0], /^example#1 Change 1 .* 10m 0s 30m 12s 1h 19m/);
  const checks = content.find("outcome-checks").filter(row => row.attrs.role === "row").slice(1).map(row => row.text);
  assert.deepStrictEqual(checks, ["CI / build 4 40m 0s 40m 0s 50%", "QA / explore 2 40m 0s 0s 50%"]);
});

test("its timers take the Pull requests view's CI-time colors: yellow after an hour, red after three", async () => {
  // A 70-minute runner wait, then a 4-hour run.
  const slow = { runs: [{ id: 1, name: "CI", jobs: [job(11, "build", at(0), at(70), at(310))] }], ready: at(0), merged: at(320) };
  const { content } = await rendered({ available: true, window_days: 14, sampled_at: at(500), pending: 0, error: null,
    pulls: [record(6, { merged: at(400) }), record(1), record(8, slow)] });
  const tones = content.find("outcome-pulls").slice(1).map(row => row.children.slice(2).map(cell => cell.children[0].className || "none"));
  // 10-minute waits and 30m 12s runs stay plain; merge waits of 6h, 1h 19m and 10m are red, yellow, plain.
  assert.deepStrictEqual(tones, [["none", "none", "age-red"], ["none", "none", "age-yellow"], ["age-yellow", "age-red", "none"]]);
  const cards = content.find("capacity-card").map(card => card.children[1].className || "none");
  assert.deepStrictEqual(cards, ["none", "none", "none", "age-yellow"]);
});

test("the view says what it is still reading, why reading paused, and when nothing is read yet", async () => {
  let { content } = await rendered({ available: true, window_days: 14, sampled_at: at(130), pending: 3, error: "github_budget_low", pulls: [] });
  const banners = content.find("source-banner").map(row => row.text);
  assert.match(banners[0], /paused while less than half of GitHub's hourly budget/);
  assert.match(banners[1], /Still reading 3 merged pull requests/);
  assert.match(content.text, /No merged pull requests in the last 7 days/);
  ({ content } = await rendered({ available: false, window_days: 14, sampled_at: null, pending: 0, error: "github_budget_low", pulls: [] }));
  assert.match(content.text, /Merged pull requests are not read yet/);
  // Paused before its first read, it says why.
  assert.match(content.find("source-banner")[0].text, /paused while less than half of GitHub's hourly budget/);
  ({ content } = await rendered(null, false));
  assert.match(content.text, /Outcomes unavailable/);
});

const DAY = 24 * HOUR;
// A pull request merged `back` days ago at local noon with the given split.
function onDay(back, wait, running, merge) {
  const noon = new Date(); noon.setHours(12, 0, 0, 0); noon.setDate(noon.getDate() - back);
  return { record: { merged_at: noon.toISOString() }, round: { wait_seconds: wait, running_seconds: running }, merge_wait_seconds: merge };
}

test("the trend gives each day of the range its medians, oldest first, and nothing on a day with no merge", () => {
  const rows = [onDay(0, 0, 600, 60), onDay(0, 120, 1200, 180), onDay(2, 30, 300, 7200), onDay(40, 1, 1, 1),
    { ...onDay(1, 0, 0, 90), round: null }];
  const list = trend.days(rows, 7, Date.now(), outcomes.median);
  assert.strictEqual(list.length, 7);
  assert.deepStrictEqual(list.map(day => day.merged), [0, 0, 0, 0, 1, 1, 2]);
  assert.deepStrictEqual(list.slice(4).map(day => [day.wait, day.running, day.merge]), [[30, 300, 7200], [null, null, 90], [60, 900, 120]]);
  assert.ok(list.every((day, index) => index === 0 || day.start > list[index - 1].start));
});

test("the trend panel draws one chart per timer and its buttons switch between 7, 14 and 30 days", () => {
  const rows = [onDay(0, 0, 600, 60), onDay(2, 30, 300, 7200), onDay(20, 10, 100, 10)];
  let redrawn = 0;
  trend.choose("30d");
  const panel = trend.panel(rows, Date.now(), outcomes.median, () => { redrawn += 1; });
  const cards = panel.find("burn-card").map(card => card.text);
  assert.deepStrictEqual(panel.find("burn-head").map(head => head.text), ["Waiting for a runner", "Checks running", "Waiting on the merge"]);
  assert.match(cards[2], /today Median over the range 1m 0s · chart max 2h 0m$/);
  // SVG items take their class as an attribute.
  const svgFind = (node, name) => [...(node.attrs?.class === name ? [node] : []), ...(node.children || []).flatMap(child => svgFind(child, name))];
  const hits = svgFind(panel, "trend-hit");
  assert.strictEqual(hits.length, 3 * 30);
  assert.match(hits[29].text, /median 0s over 1 merged pull request$/);
  assert.match(hits[28].text, /: no merged pull requests$/);
  const buttons = panel.find("range-buttons")[0].children;
  assert.deepStrictEqual(buttons.map(button => [button.text, button.attrs["aria-pressed"]]), [["7d", "false"], ["14d", "false"], ["30d", "true"]]);
  buttons[0].listeners.click();
  assert.strictEqual(redrawn, 1);
  const week = trend.panel(rows, Date.now(), outcomes.median, () => {});
  assert.strictEqual(svgFind(week, "trend-hit").length, 3 * 7);
  assert.strictEqual(svgFind(week, "burn-line").length, 3 * 2);
  // The 20-day-old merge is outside the week: the merge wait median is the last two days' only.
  assert.match(week.find("burn-card")[2].text, /Median over the range 1h 0m/);
  trend.choose("30d");
});

test("a merge belongs to its local calendar day: the last second of yesterday is yesterday's, midnight is today's", () => {
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const merge = (time, merge) => ({ record: { merged_at: new Date(time).toISOString() }, round: null, merge_wait_seconds: merge });
  const firstDay = new Date(midnight); firstDay.setDate(firstDay.getDate() - 6);
  const rows = [merge(midnight.getTime() - 1000, 10), merge(midnight.getTime(), 20), merge(firstDay.getTime(), 30), merge(firstDay.getTime() - 1000, 40)];
  const list = trend.days(rows, 7, Date.now(), outcomes.median);
  assert.deepStrictEqual(list.map(day => day.merge), [30, null, null, null, null, 10, 20]);
  assert.strictEqual(list[0].start, firstDay.getTime());
  // A day whose pull requests have no round says so instead of claiming nothing merged.
  trend.choose("7d");
  const svgFind = (node, name) => [...(node.attrs?.class === name ? [node] : []), ...(node.children || []).flatMap(child => svgFind(child, name))];
  const hits = svgFind(trend.panel(rows, Date.now(), outcomes.median, () => {}), "trend-hit");
  assert.match(hits[6].text, /1 merged pull request, none with this time to measure$/);
  trend.choose("30d");
});
