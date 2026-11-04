// The layout record's own tests, with a stand-in for the Playwright page. Run by
// tests/test_qae_ui_audit.py, which also measures real pages where a browser is installed.
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const hook = require("./ui-audit.js");

const finding = (rule, at, detail = "d") => ({ rule, at, text: "Open", detail });

// A tab showing `url` whose audit returns each of `answers` in turn (an Error is a measure the
// tab's navigation interrupted); `fire` runs its listeners, `lines` reads back the record.
function tab(url, answers, width = 1280) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-ui-audit-"));
  const listeners = {};
  let binding;
  const page = {
    url: () => url,
    viewportSize: () => ({ width, height: 800 }),
    mainFrame: () => "main",
    on: (event, listener) => { listeners[event] = listener; },
    exposeBinding: async (name, fn) => { binding = fn; },
    addInitScript: async () => {},
    evaluate: async () => {
      const answer = answers.shift() || [];
      if (answer instanceof Error) throw answer;
      return answer;
    },
  };
  const argv = process.argv;
  return {
    page,
    start: async () => {
      process.argv = ["node", "cli.js", "--output-dir", dir];
      try { await hook.default({ page }); } finally { process.argv = argv; }
    },
    spinner: () => binding(),
    fire: event => listeners[event](event === "framenavigated" ? "main" : undefined),
    exists: () => fs.existsSync(path.join(dir, "ui-audit.jsonl")),
    lines: () => {
      const file = path.join(dir, "ui-audit.jsonl");
      return fs.existsSync(file) ? fs.readFileSync(file, "utf8").split("\n").filter(Boolean).map(JSON.parse) : [];
    },
  };
}
const settle = ms => new Promise(resolve => setTimeout(resolve, ms));

test("a finding is recorded once per page, width, rule and element, with the page's query dropped", async () => {
  const written = [];
  const page = { url: () => "https://site.test/quotes?view=1#top", viewportSize: () => ({ width: 375 }) };
  const twice = [finding("touching", "td > button"), finding("touching", "td > button", "other px"), finding("edge", "td > a")];
  await hook.record({ ...page, evaluate: async () => twice }, line => written.push(line), new Set());
  assert.deepStrictEqual(written.map(line => [line.page, line.width, line.rule, line.at]),
    [["https://site.test/quotes", 375, "touching", "td > button"], ["https://site.test/quotes", 375, "edge", "td > a"]]);
});

test("a measure the tab's navigation interrupts writes nothing and does not throw", async () => {
  const written = [];
  await hook.record({ url: () => "https://site.test/", viewportSize: () => null, evaluate: async () => { throw new Error("Execution context was destroyed"); } },
    line => written.push(line), new Set());
  assert.deepStrictEqual(written, []);
});

test("a clean tab still leaves a record, empty, so the gate can tell clean pages from a hook never loaded", async () => {
  const t = tab("https://site.test/clean", [[]]);
  await t.start();
  await settle(1100);
  assert.deepStrictEqual(t.lines(), []);
  assert.ok(t.exists());
});

test("the hook measures after the first load and again after a navigation, never repeating a line", async () => {
  const t = tab("https://site.test/a", [[finding("edge", "div > button")], [finding("edge", "div > button"), finding("row-height", "form > label")]]);
  await t.start();
  await settle(1100);
  t.fire("framenavigated");
  await settle(1100);
  assert.deepStrictEqual(t.lines().map(line => line.rule), ["edge", "row-height"]);
});

test("a spinner appearing is measured at once, not a second later", async () => {
  const t = tab("https://site.test/save", [[], [finding("spinner-off-center", "button > span")]]);
  await t.start();
  await settle(1100);
  t.spinner();
  await settle(50);
  assert.deepStrictEqual(t.lines().map(line => line.rule), ["spinner-off-center"]);
});

test("the audit is self-contained: run as the page script on an empty page, it needs only the DOM and finds nothing", () => {
  const vm = require("node:vm");
  const page = { querySelectorAll: () => [], createElement: () => ({ getContext: () => ({ measureText: () => ({ width: 0 }) }) }) };
  assert.strictEqual(typeof require("./ui-audit-page.js"), "function");
  assert.strictEqual(JSON.stringify(vm.runInNewContext(hook.script, { document: page, getComputedStyle: () => ({}) })), "[]");
});

test("the shell form refuses no URL or a width that is not a number", () => {
  const { spawnSync } = require("node:child_process");
  for (const args of [[], ["--width", "wide", "https://site.test/"]]) {
    const run = spawnSync(process.execPath, [path.join(__dirname, "ui-audit.js"), ...args], { encoding: "utf8" });
    assert.strictEqual(run.status, 2, run.stderr);
    assert.match(run.stderr, /^usage: /);
  }
});
