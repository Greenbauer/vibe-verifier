// The layout record's own tests, with a stand-in for the Playwright page. Run by tests/test_ui_audit.py;
// tests/test_qae_ui_audit.py measures real pages where a browser is installed.
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const hook = require("./ui-audit.js");

const finding = (rule, at, detail = "d") => ({ rule, at, text: "Open", detail });

// A tab showing `url` whose audit returns each of `answers` in turn (an Error is a measure the
// tab's navigation interrupted), and whose copy finder returns each of `copies`; `fire` runs its
// listeners, `lines` and `copy` read back the two records. Its window holds no control with only an icon.
function tab(url, answers, width = 1280, copies = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-ui-audit-"));
  const listeners = {}, bindings = {};
  const page = {
    url: () => url,
    viewportSize: () => ({ width, height: 800 }),
    mainFrame: () => "main",
    on: (event, listener) => { listeners[event] = listener; },
    exposeBinding: async (name, fn) => { bindings[name] = fn; },
    addInitScript: async () => {},
    evaluate: async script => {
      if (script === hook.iconScript || script === require("./modal-dismiss.js").openScript) return [];
      const answer = (script === hook.copyScript ? copies : answers).shift() || [];
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
    spinner: () => bindings.__vibeVerifierMeasure({}, 0),
    message: () => bindings.__vibeVerifierMeasure({}, 500),
    fire: event => listeners[event](event === "framenavigated" ? "main" : undefined),
    exists: () => fs.existsSync(path.join(dir, "ui-audit.jsonl")),
    lines: () => read(path.join(dir, "ui-audit.jsonl")),
    copy: () => read(path.join(dir, "copy.jsonl")),
    copyExists: () => fs.existsSync(path.join(dir, "copy.jsonl")),
  };
}
const read = file => fs.existsSync(file) ? fs.readFileSync(file, "utf8").split("\n").filter(Boolean).map(JSON.parse) : [];
const block = (text, kind = "sentence") => ({ kind, text, at: "div > p" });
const settle = ms => new Promise(resolve => setTimeout(resolve, ms));

test("a finding is recorded once per page, width, rule and element, with the page's query dropped", async () => {
  const written = [];
  const page = { url: () => "https://site.test/quotes?view=1#top", viewportSize: () => ({ width: 375 }) };
  const twice = [finding("touching", "td > button"), finding("touching", "td > button", "other px"), finding("edge", "td > a")];
  await hook.record({ ...page, evaluate: async () => twice }, line => written.push(line), new Set());
  assert.deepStrictEqual(written.map(line => [line.page, line.width, line.rule, line.at]),
    [["https://site.test/quotes", 375, "touching", "td > button"], ["https://site.test/quotes", 375, "edge", "td > a"]]);
});

test("a finding is labelled with the width it was measured at, though the window changes while it runs", async () => {
  const written = [];
  let size = { width: 375 };
  const page = { url: () => "https://site.test/", viewportSize: () => size,
    evaluate: async () => { size = { width: 1280 }; return [finding("touching", "td > button")]; } };
  await hook.record(page, line => written.push(line), new Set());
  assert.deepStrictEqual(written.map(line => line.width), [375]);
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

// A page whose window holds the icon controls of each list in `looks` in turn (the last repeated), and whose
// screenshots answer `frames` in turn; `shots` counts them.
function iconTab(looks, frames) {
  const queue = [...frames], page = { shots: 0 };
  let look = 0;
  return Object.assign(page, {
    url: () => "https://site.test/quotes?id=4",
    viewportSize: () => ({ width: 1280 }),
    evaluate: async script => (script === hook.iconScript ? looks[Math.min(look++, looks.length - 1)] : []),
    screenshot: async () => { page.shots++; return queue.shift(); },
  });
}
// A 16px control at (x, 0) and a screenshot of it: white, with a 4px square of ink whose left edge is at `left`, so
// its middle sits left + 2 - 8 px right of the control's.
const control = (at, x = 0) => ({ at, mark: "svg.", text: at, clip: { x, y: 0, width: 16, height: 16 }, center: { x: x + 8, y: 8 },
  scan: { left: x + 1, top: 1, right: x + 15, bottom: 15, radius: [0, 0, 0, 0] } });
function shot(left) {
  const zlib = require("node:zlib");
  const chunk = (kind, body) => {
    const head = Buffer.alloc(8);
    head.writeUInt32BE(body.length);
    head.write(kind, 4, "latin1");
    return Buffer.concat([head, body, Buffer.alloc(4)]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(16);
  header.writeUInt32BE(16, 4);
  header[8] = 8;
  header[9] = 2;
  const rows = [...Array(16).keys()].map(y => Buffer.from([0, ...[...Array(16).keys()].flatMap(x => (x >= left && x < left + 4 && y >= 6 && y < 10 ? [0, 0, 0] : [255, 255, 255]))]));
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(rows))), chunk("IEND", Buffer.alloc(0))]);
}
const OFF = 9, MIDDLE = 6;   // a square 3px right of the middle, and one on it

test("an icon off its control's middle in every frame is recorded, once per page, width and control", async () => {
  // Frames are taken a control at a time: save, close, save, close, save, close. Close is on the middle once.
  const page = iconTab([[control("button.save"), control("button.close", 20)]], [OFF, OFF, OFF, MIDDLE, OFF, OFF].map(shot));
  const written = [], sampled = new Set();
  await hook.recordIcons(page, line => written.push(line), new Set(), sampled);
  assert.deepStrictEqual(written.map(line => [line.page, line.width, line.rule, line.at]), [["https://site.test/quotes", 1280, "icon-off-center", "button.save"]]);
  assert.match(written[0].detail, /in each of 3 frames: 3\.0px right; 3\.0px right; 3\.0px right$/);
  // Both are sampled: a second measure takes no screenshot and writes nothing more.
  await hook.recordIcons(page, line => written.push(line), new Set(), sampled);
  assert.deepStrictEqual([page.shots, written.length], [6, 1]);
  // The same control showing another mark (its icon swapped for a spinner) is sampled again.
  const swapped = iconTab([[{ ...control("button.save"), mark: "span.spinner" }]], [OFF, OFF, OFF].map(shot));
  await hook.recordIcons(swapped, line => written.push(line), new Set(), sampled);
  assert.strictEqual(swapped.shots, 3);
});

test("a control that moves while it is sampled is left for a later measure, and a measure samples at most four", async () => {
  const moved = { ...control("button.save"), clip: { x: 1, y: 0, width: 16, height: 16 } };
  const page = iconTab([[control("button.save")], [moved]], [OFF, OFF, OFF].map(shot));
  const written = [], sampled = new Set();
  await hook.recordIcons(page, line => written.push(line), new Set(), sampled);
  assert.deepStrictEqual([written, sampled.size], [[], 0]);
  const many = iconTab([[1, 2, 3, 4, 5].map(n => control("button.n" + n, n * 20))], []);
  await hook.recordIcons(many, () => {}, new Set(), new Set());
  assert.strictEqual(many.shots, 12);
});

test("the copy record keeps each text once per page, at the width it was first read", async () => {
  const written = [];
  const copied = new Set();
  const at = (url, width, blocks) => ({ url: () => url, viewportSize: () => ({ width }), evaluate: async () => blocks });
  await hook.recordCopy(at("https://site.test/users?tab=2", 1280, [block("Invite a user."), block("Save", "label")]), line => written.push(line), copied);
  await hook.recordCopy(at("https://site.test/users", 375, [block("Invite a user."), block("Cancel", "label")]), line => written.push(line), copied);
  await hook.recordCopy(at("https://site.test/team", 1280, [block("Save", "label")]), line => written.push(line), copied);
  assert.deepStrictEqual(written.map(line => [line.page, line.width, line.text]), [
    ["https://site.test/users", 1280, "Invite a user."], ["https://site.test/users", 1280, "Save"],
    ["https://site.test/users", 375, "Cancel"], ["https://site.test/team", 1280, "Save"]]);
});

test("a dialog opening is read half a second later, with its copy, and a clean start leaves an empty copy record", async () => {
  const t = tab("https://site.test/users", [[], []], 1280, [[], [block("They get an invite email.")]]);
  await t.start();
  await settle(1100);
  assert.ok(t.copyExists());
  assert.deepStrictEqual(t.copy(), []);
  t.message();
  await settle(200);
  assert.deepStrictEqual(t.copy(), []);
  await settle(450);
  assert.deepStrictEqual(t.copy().map(line => line.text), ["They get an invite email."]);
});

test("the copy finder is self-contained: run as the page script on an empty page, it finds nothing", () => {
  const vm = require("node:vm");
  const page = { body: {}, querySelectorAll: () => [], createTreeWalker: () => ({ nextNode: () => null }) };
  assert.strictEqual(typeof require("./copy-page.js"), "function");
  assert.strictEqual(JSON.stringify(vm.runInNewContext(hook.copyScript, { document: page, NodeFilter: { SHOW_TEXT: 4 } })), "[]");
});

test("the audit is self-contained: run as the page script on an empty page, it needs only the DOM and finds nothing", () => {
  const vm = require("node:vm");
  const page = { querySelectorAll: () => [], createElement: () => ({ getContext: () => ({ measureText: () => ({ width: 0 }) }) }),
    documentElement: { clientWidth: 1280, clientHeight: 900 } };
  assert.strictEqual(typeof require("./ui-audit-page.js"), "function");
  assert.strictEqual(typeof require("./row-page.js"), "function");
  assert.ok(hook.script.includes("function rowRules("));   // the row rules run in the same script
  assert.strictEqual(typeof require("./glyph-page.js"), "function");
  assert.ok(hook.script.includes("function glyphRules("));   // the badge rule runs in the same script
  assert.strictEqual(typeof require("./name-page.js"), "function");
  assert.ok(hook.script.includes("function nameRules("));   // and so does the duplicate-name rule
  assert.strictEqual(typeof require("./wrap-page.js"), "function");
  assert.ok(hook.script.includes("function wrapRules("));   // and so does the wrapped-label rule
  assert.strictEqual(typeof require("./scroll-page.js"), "function");
  assert.ok(hook.script.includes("function scrollRules("));   // and so does the table-scroll rule
  assert.strictEqual(typeof require("./gutter-page.js"), "function");
  assert.ok(hook.script.includes("function gutterRules("));   // and so does the uneven-gutter rule
  assert.strictEqual(typeof require("./inset-page.js"), "function");
  assert.ok(hook.script.includes("function insetRules("));   // and so does the nested-inset rule
  assert.strictEqual(typeof require("./field-page.js"), "function");
  assert.ok(hook.script.includes("function crampedRules("));   // and so does the cramped-field rule
  assert.strictEqual(JSON.stringify(vm.runInNewContext(hook.script, { document: page, getComputedStyle: () => ({}) })), "[]");
  // The controls icon-off-center screenshots are found by a script of the same files.
  assert.strictEqual(typeof require("./icon-page.js"), "function");
  assert.ok(hook.iconScript.includes("function iconControls("));
  assert.strictEqual(JSON.stringify(vm.runInNewContext(hook.iconScript, { document: page, getComputedStyle: () => ({}) })), "[]");
});

test("the rule files run as one script, so no two declare the same top-level name", () => {
  // A second `function lines` in gutter-page.js would have replaced wrap-page.js's, silently breaking wrapped-label.
  // modal-page.js runs beside ui-audit-page.js and name-page.js (modal-dismiss.js), and is held to every file's names.
  const files = ["ui-audit-page.js", "row-page.js", "glyph-page.js", "name-page.js", "wrap-page.js", "scroll-page.js", "gutter-page.js", "inset-page.js", "field-page.js", "icon-page.js", "modal-page.js"];
  const seen = new Map();
  for (const file of files) {
    for (const [, name] of fs.readFileSync(path.join(__dirname, file), "utf8").matchAll(/^(?:function|const|let)\s+([A-Za-z_$][\w$]*)/gm)) {
      assert.ok(!seen.has(name), `${name} is declared in ${seen.get(name)} and ${file}`);
      seen.set(name, file);
    }
  }
  assert.ok(files.slice(0, -1).every(file => hook.script.includes(fs.readFileSync(path.join(__dirname, file), "utf8"))));
});

test("the shell form refuses no URL or a width that is not a number", () => {
  const { spawnSync } = require("node:child_process");
  for (const args of [[], ["--width", "wide", "https://site.test/"]]) {
    const run = spawnSync(process.execPath, [path.join(__dirname, "ui-audit.js"), ...args], { encoding: "utf8" });
    assert.strictEqual(run.status, 2, run.stderr);
    assert.match(run.stderr, /^usage: /);
  }
});
