// The secret names' own tests, with stand-ins for the Playwright classes. Run by
// tests/test_qae_secret_names.py, which also drives the real browser where one is installed.
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const hook = require("./secret-names.js");

// A browser of stand-in classes, fresh for each test because the hook wraps the classes themselves.
// Every text-entry call lands in `sent` as [class.method, ...arguments]; `tab()` is a new tab of it.
function browser() {
  const sent = [];
  const record = (name, value) => function (...args) { sent.push([name, ...args]); return value; };
  class Frame {
    async evaluateHandle(expression) { sent.push(["Frame.evaluateHandle", expression]); return new ElementHandle(); }
  }
  Frame.prototype.fill = record("Frame.fill", "filled");
  Frame.prototype.type = record("Frame.type");
  class ElementHandle { async dispose() {} }
  ElementHandle.prototype.fill = record("ElementHandle.fill");
  ElementHandle.prototype.type = record("ElementHandle.type");
  class Keyboard {}
  Keyboard.prototype.type = record("Keyboard.type");
  Keyboard.prototype.insertText = record("Keyboard.insertText");
  const tab = () => { const frame = new Frame(); return { mainFrame: () => frame, keyboard: new Keyboard() }; };
  return { sent, tab, Frame, ElementHandle, Keyboard };
}

// The hook, run on `page` in a playwright-mcp given `config` as its --config file (none: no --config).
async function open(page, config) {
  const argv = process.argv;
  process.argv = ["node", "cli.js", "--output-dir", "qae-artifacts"];
  if (config !== undefined) {
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "vv-secret-names-")), "mcp.json");
    fs.writeFileSync(file, JSON.stringify(config));
    process.argv.push("--config", file);
  }
  try {
    await hook.default({ page });
  } finally {
    process.argv = argv;
  }
}

const LOGIN = { secrets: { QAE_PASSWORD: "s3cret", VV_COOKIE_1: "cookie-value" } };

test("a name entered through any text-entry call is typed as its value", async () => {
  const { sent, tab, ElementHandle } = browser();
  const page = tab();
  await open(page, LOGIN);
  sent.length = 0;
  await page.mainFrame().fill("input[type=password]", "QAE_PASSWORD", { strict: true });
  await page.mainFrame().type("input", "QAE_PASSWORD");
  await new ElementHandle().fill("QAE_PASSWORD");
  await new ElementHandle().type("VV_COOKIE_1");
  await page.keyboard.type("QAE_PASSWORD", { delay: 5 });
  await page.keyboard.insertText("QAE_PASSWORD");
  assert.deepStrictEqual(sent, [
    ["Frame.fill", "input[type=password]", "s3cret", { strict: true }],
    ["Frame.type", "input", "s3cret"],
    ["ElementHandle.fill", "s3cret"],
    ["ElementHandle.type", "cookie-value"],
    ["Keyboard.type", "s3cret", { delay: 5 }],
    ["Keyboard.insertText", "s3cret"],
  ]);
});

test("only text that is exactly a name is replaced, and never a selector", async () => {
  const { sent, tab } = browser();
  const page = tab();
  await open(page, LOGIN);
  sent.length = 0;
  for (const text of ["my QAE_PASSWORD", "QAE_PASSWORD ", "qae_password", "", "s3cret", "toString"]) await page.keyboard.type(text);
  await page.mainFrame().fill("QAE_PASSWORD", "qae@example.test");
  assert.deepStrictEqual(sent.map(call => call[1]), ["my QAE_PASSWORD", "QAE_PASSWORD ", "qae_password", "", "s3cret", "toString", "QAE_PASSWORD"]);
});

test("a wrapped call still answers what the call answers", async () => {
  const { tab } = browser();
  const page = tab();
  await open(page, LOGIN);
  assert.strictEqual(await page.mainFrame().fill("input", "QAE_PASSWORD"), "filled");
});

test("the first tab wraps the classes for every later tab", async () => {
  const { sent, tab } = browser();
  await open(tab(), LOGIN);
  const later = tab();
  await open(later, LOGIN);
  // One handle was asked for, by the first tab; a later tab may be mid-navigation and is not asked.
  assert.deepStrictEqual(sent, [["Frame.evaluateHandle", "document"]]);
  await later.keyboard.type("QAE_PASSWORD");
  assert.deepStrictEqual(sent[1], ["Keyboard.type", "s3cret"]);
});

test("without secrets nothing is wrapped", async () => {
  for (const config of [undefined, {}, { secrets: {} }, { secrets: { QAE_PASSWORD: "" } }]) {
    const { sent, tab, Frame } = browser();
    const fill = Frame.prototype.fill;
    const page = tab();
    await open(page, config);
    assert.strictEqual(Frame.prototype.fill, fill);
    await page.keyboard.type("QAE_PASSWORD");
    assert.deepStrictEqual(sent, [["Keyboard.type", "QAE_PASSWORD"]]);
  }
});

test("a name whose value is empty is typed as written, as playwright-mcp's own tools do", async () => {
  const { sent, tab } = browser();
  const page = tab();
  await open(page, { secrets: { QAE_PASSWORD: "s3cret", QAE_OTHER: "" } });
  sent.length = 0;
  await page.keyboard.type("QAE_OTHER");
  assert.deepStrictEqual(sent, [["Keyboard.type", "QAE_OTHER"]]);
});

test("a build without one of the calls stops the tab from opening", async () => {
  const { tab, Keyboard } = browser();
  delete Keyboard.prototype.insertText;
  await assert.rejects(open(tab(), LOGIN), /no Keyboard\.insertText to resolve secret names in/);
});

test("the config file is the one playwright-mcp was given, in either spelling", () => {
  assert.strictEqual(hook.configFile(["node", "cli.js", "--config", "/tmp/mcp.json", "--headless"]), "/tmp/mcp.json");
  assert.strictEqual(hook.configFile(["node", "cli.js", "--config=/tmp/mcp.json"]), "/tmp/mcp.json");
  assert.strictEqual(hook.configFile(["node", "cli.js", "--headless"]), undefined);
});
