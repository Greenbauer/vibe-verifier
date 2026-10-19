// Renders the CI timeline (timeline.js) against a minimal DOM: which checks become lanes, the
// critical path, the row's lines and scale, the expanded rows and their steps. Run by
// tests/test_dashboard_timeline.py.
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

// CI: lint, then a failing test, then deploy, which GitHub created when the test finished.
// QAE: explore waited 38 minutes for a runner after lint finished; verify was created when explore
// finished and is still running; settle has been queued for 10 seconds; skip never ran.
const pull = {
  repository: "octocat/example", number: 7,
  checks: [check(1, "lint", "success"), check(2, "test", "failed"), check(3, "deploy", "success"),
    check(4, "explore", "success"), check(5, "skip", "skipped"), check(6, "verify", "pending", "in_progress"),
    check(7, "settle", "pending", "queued"),
    { id: 99, name: "Vercel Preview Comments", category: "success", status: "completed", started_at: at(12), completed_at: at(12) }],
  statuses: [{ id: 50, name: "preview", category: "success", started_at: at(12, 30), completed_at: at(12, 30),
    details_url: "https://vercel.com/octocat/example" }],
  runs: [
    { id: 10, name: "CI", jobs: [job(1, "lint", at(0), at(0, 5), at(2)),
      job(2, "test", at(0), at(0, 10), at(10), { steps: [
        { name: "Checkout", category: "success", status: "completed", started_at: at(0, 10), completed_at: at(0, 20) },
        { name: "Unit tests", category: "failed", status: "completed", started_at: at(0, 20), completed_at: at(9, 50) },
        { name: "Upload report", category: "skipped", status: "completed", started_at: null, completed_at: null }] }),
      job(3, "deploy", at(10), at(10, 5), at(12))] },
    { id: 20, name: "QAE", jobs: [job(4, "explore", at(2), at(40), at(45)), job(5, "skip", at(2), at(2), at(2)),
      job(6, "verify", at(45), at(45, 10), null), job(7, "settle", at(49, 50), at(49, 50), null)] }]
};
const short = { repository: "octocat/example", number: 8, statuses: [], checks: [check(1, "lint", "success")],
  runs: [{ id: 30, name: "CI", jobs: [job(1, "lint", at(0), at(0, 10), at(10))] }] };
const edge = bar => parseFloat(bar.style.get("left")) + parseFloat(bar.style.get("width"));

test("every check with a time is a lane, grouped by workflow in the order first queued", () => {
  const line = timeline.timeline(pull, NOW);
  assert.deepStrictEqual(line.groups.map(group => [group.name, group.lanes.map(lane => lane.name)]), [
    ["CI", ["lint", "test", "deploy"]], ["QAE", ["explore", "verify", "settle"]],
    ["Other checks", ["Vercel Preview Comments"]], ["Commit statuses", ["preview"]]]);
  const lane = name => line.lanes.find(item => item.name === name);
  assert.deepStrictEqual([lane("verify").state, lane("verify").ended, lane("settle").state, lane("settle").started],
    ["running", null, "waiting", null]);
  assert.strictEqual(line.span, 50 * 60 * 1000);
  assert.strictEqual(line.running, true);
});

test("the critical path follows each job back to the finish that created it", () => {
  const line = timeline.timeline(pull, NOW);
  assert.deepStrictEqual(line.lanes.filter(lane => lane.critical).map(lane => lane.name).sort(), ["explore", "lint", "verify"]);
  // A job that finished long before another was created did not create it.
  const later = { checks: [check(1, "build", "success"), check(2, "e2e", "success")], statuses: [],
    runs: [{ id: 1, name: "CI", jobs: [job(1, "build", at(0), at(0), at(5)), job(2, "e2e", at(20), at(20), at(30))] }] };
  assert.deepStrictEqual(timeline.timeline(later, NOW).lanes.filter(lane => lane.critical).map(lane => lane.name), ["e2e"]);
});

test("the row draws one line per workflow, real runner waits, failures and now", () => {
  const box = timeline.mini(pull, NOW, null);
  assert.strictEqual(box.find("timeline-line").length, 3);
  assert.strictEqual(box.attrs["aria-label"], "CI timeline: 7 checks over 50m 0s, longest runner wait 38m 0s, 1 failed");
  const waits = box.find("bar-waiting");
  assert.deepStrictEqual(waits.map(bar => [bar.style.get("left"), bar.style.get("width"), bar.attrs.title]),
    [["4.000%", "76.000%", "explore: waited 38m 0s for a runner, ran 5m 0s"]]);
  assert.strictEqual(box.find("bar-failed").length, 1);
  assert.strictEqual(box.find("timeline-now")[0].style.get("left"), "100.000%");
  // On the QAE line the wait is drawn first, under the runs.
  const qae = box.find("timeline-line")[1].children.map(bar => bar.className);
  assert.deepStrictEqual(qae, ["timeline-bar bar-waiting", "timeline-bar bar-success", "timeline-bar bar-running"]);
});

test("rows share one time scale unless each row is fit", () => {
  assert.strictEqual(timeline.sharedScale([pull, short], NOW), 50 * 60 * 1000);
  const shared = timeline.mini(short, NOW, timeline.sharedScale([pull, short], NOW));
  const fitted = timeline.mini(short, NOW, null);
  assert.strictEqual(Math.max(...shared.find("timeline-bar").map(edge)), 20);
  assert.strictEqual(Math.max(...fitted.find("timeline-bar").map(edge)), 100);
  assert.strictEqual(timeline.sharedScale([{ checks: [], statuses: [], runs: [] }], NOW), null);
});

test("a dense head draws every workflow as a line, sized by the stylesheet", () => {
  const runs = Array.from({ length: 20 }, (_, index) => ({ id: index, name: `wf-${index}`, jobs: [job(index, `job-${index}`, at(0), at(0), at(1))] }));
  const box = timeline.mini({ checks: runs.map(run => check(run.id, run.name, "success")), statuses: [], runs }, NOW, null);
  const lines = box.find("timeline-line");
  assert.strictEqual(lines.length, 20);
  assert.ok(lines.every(line => line.style.get("height") === undefined));
});

test("the expanded view lists every job with its time, bold on the critical path, links only to the owner's GitHub", () => {
  let renders = 0;
  const options = { id: "timeline-7", key: "pull-7", owner: "octocat", rerender: () => { renders += 1; } };
  const box = timeline.detail(pull, NOW, options);
  assert.strictEqual(box.attrs.id, "timeline-7");
  assert.deepStrictEqual(box.find("timeline-group").map(node => node.text), ["CI", "QAE", "Other checks", "Commit statuses"]);
  const rows = box.find("timeline-row").filter(row => !row.className.includes("timeline-axis"));
  assert.deepStrictEqual(rows.map(row => [row.find("timeline-name")[0].text.replace(/^▸ /, ""), row.find("timeline-time")[0].text,
    row.className.includes("critical")]), [
    ["lint", "1m 55s", true], ["test", "9m 50s", false], ["deploy", "1m 55s", false],
    ["explore", "5m 0s", true], ["verify", "4m 50s so far", true], ["settle", "waiting 10s", false],
    ["Vercel Preview Comments", "", false], ["preview", "", false]]);
  // A 10-second queue is GitHub's normal pickup, so it is not drawn.
  assert.strictEqual(rows[5].find("timeline-bar").length, 0);
  // The other app's check has no link and the status's Vercel page is not the owner's GitHub.
  assert.deepStrictEqual(rows.map(row => row.find("timeline-link").map(link => link.attrs.href)),
    [1, 2, 3, 4, 6, 7].map(id => [`https://github.com/octocat/example/actions/runs/1/job/${id}`]).concat([[], []]));
  assert.deepStrictEqual(box.find("timeline-tick").map(tick => tick.text), ["0", "10m", "20m", "30m", "40m", "50m"]);

  const toggle = rows[1].find("timeline-name")[0];
  assert.deepStrictEqual([toggle.tag, toggle.attrs["aria-expanded"]], ["button", "false"]);
  toggle.handlers.click();
  assert.strictEqual(renders, 1);
  const open = timeline.detail(pull, NOW, options);
  const steps = open.find("timeline-step");
  assert.deepStrictEqual(steps.map(row => [row.find("timeline-name")[0].text, row.find("timeline-time")[0].text]),
    [["Checkout", "10s"], ["Unit tests", "9m 30s"]]);
  assert.strictEqual(open.find("timeline-step-scale")[0].text, "Steps on this job's own 9m 50s scale");
  assert.deepStrictEqual(steps[1].find("timeline-bar")[0].className, "timeline-bar bar-failed");
  assert.strictEqual(steps[1].find("timeline-bar")[0].style.get("left"), `${(10 / 590 * 100).toFixed(3)}%`);
});

test("a head with no timed check has no row timeline and says so when opened", () => {
  const bare = { checks: [check(1, "lint", "pending", "queued")], statuses: [], runs: [], expected: [] };
  assert.strictEqual(timeline.mini(bare, NOW, null), null);
  assert.strictEqual(timeline.detail(bare, NOW, { id: "x", key: "k", owner: "octocat", rerender() {} }).text,
    "No check on this head has a start time yet.");
});

test("axis ticks land on round times, at most six", () => {
  assert.deepStrictEqual(timeline.ticks(50 * 60 * 1000), [0, 600, 1200, 1800, 2400, 3000]);
  assert.deepStrictEqual(timeline.ticks(40 * 1000), [0, 10, 20, 30, 40]);
  assert.deepStrictEqual(timeline.ticks(77 * 60 * 1000), [0, 900, 1800, 2700, 3600, 4500]);
});

test("a commit status is a point at its last update: never a runner wait, never open, never stretching a row", () => {
  const pending = { checks: [check(1, "build", "success")], runs: [{ id: 1, name: "CI", jobs: [job(1, "build", at(0), at(0), at(10))] }],
    statuses: [{ id: 9, name: "deploy", category: "pending", status: "pending", started_at: at(-180), completed_at: at(-180) }] };
  const line = timeline.timeline(pending, NOW);
  const status = line.lanes.find(lane => lane.kind === "status");
  assert.deepStrictEqual([status.started, status.ended, line.running], [status.queued, status.queued, false]);
  assert.strictEqual(timeline.sharedScale([pending], NOW), 10 * 60 * 1000);
  // The row does not draw statuses, so fitting the row fits the build, not the status three hours earlier.
  assert.strictEqual(Math.max(...timeline.mini(pending, NOW, null).find("timeline-bar").map(edge)), 100);
  const opened = timeline.detail(pending, NOW, { id: "x", key: "k", owner: "octocat", rerender() {} });
  const row = opened.find("timeline-row").find(item => item.find("timeline-name")[0]?.text === "deploy");
  assert.deepStrictEqual([row.find("bar-waiting").length, row.find("timeline-time")[0].text,
    row.find("timeline-time")[0].attrs.title], [0, "", "deploy: Pending"]);
});

test("another app's check run is never on the critical path", () => {
  const vercel = { statuses: [], checks: [check(1, "build", "success"), check(2, "e2e", "success"),
    { id: 3, name: "Vercel", category: "success", status: "completed", started_at: at(1), completed_at: at(30) }],
  runs: [{ id: 1, name: "CI", jobs: [job(1, "build", at(0), at(0), at(5)), job(2, "e2e", at(5), at(5), at(25))] }] };
  assert.deepStrictEqual(timeline.timeline(vercel, NOW).lanes.filter(lane => lane.critical).map(lane => lane.name).sort(), ["build", "e2e"]);
});

test("a wait of exactly 30 seconds is GitHub's normal pickup; one second more is drawn", () => {
  const waits = { statuses: [], checks: [check(1, "a", "success"), check(2, "b", "success")],
    runs: [{ id: 1, name: "CI", jobs: [job(1, "a", at(0), at(0, 30), at(5)), job(2, "b", at(0), at(0, 31), at(5))] }] };
  const bars = timeline.mini(waits, NOW, null).find("bar-waiting");
  assert.deepStrictEqual(bars.map(bar => bar.attrs.title), ["b: waited 31s for a runner, ran 4m 29s"]);
});

test("another app's queued check says queued, never a runner wait", () => {
  const external = { statuses: [], runs: [], checks: [{ id: 40, name: "ext-ci", category: "pending", status: "queued", started_at: at(40) }] };
  const lane = timeline.timeline(external, NOW).lanes[0];
  assert.deepStrictEqual([lane.kind, lane.state], ["check", "waiting"]);
  assert.strictEqual(timeline.mini(external, NOW, null).find("bar-waiting").length, 0);
  const row = timeline.detail(external, NOW, { id: "x", key: "k", owner: "octocat", rerender() {} }).find("timeline-row")[1];
  assert.deepStrictEqual([row.find("bar-waiting").length, row.find("timeline-time")[0].text, row.find("timeline-time")[0].attrs.title],
    [0, "queued 10m 0s", "ext-ci: queued 10m 0s"]);
});
