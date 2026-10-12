// The page record's own tests, with a stand-in for the Playwright page. Run by
// tests/test_qae_console_pages.py, which also drives the real browser where one is installed.
const assert = require("node:assert");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const hook = require("./console-pages.js");

const message = (type, text, url = "https://cdn.example/app.js", lineNumber = 12) =>
  ({ type: () => type, text: () => text, location: () => ({ url, lineNumber }) });
// An uncaught exception or rejection: an Error, or any other value a page threw.
const thrown = value => ({ thrown: value });
const isException = logged => "thrown" in logged;
const digest = line => crypto.createHash("sha256").update(line).digest("hex");

// The hook, run on a tab showing `url` that already logged `earlier`: `log` is the tab logging one
// more message, `navigate` moves it to another page, `record` reads back what the hook wrote.
async function tab(url, earlier = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-console-pages-"));
  const out = path.join(dir, "not-yet-made");
  const listeners = { console: [], pageerror: [] };
  const page = {
    url: () => url,
    on: (event, listener) => listeners[event].push(listener),
    consoleMessages: async () => earlier.filter(logged => !isException(logged)),
    pageErrors: async () => earlier.filter(isException).map(logged => logged.thrown),
  };
  const argv = process.argv;
  process.argv = ["node", "cli.js", "--output-dir", out];
  try {
    await hook.default({ page });
    return {
      log: logged => listeners[isException(logged) ? "pageerror" : "console"].forEach(listener => listener(isException(logged) ? logged.thrown : logged)),
      record: () => fs.existsSync(path.join(out, "console-pages.jsonl"))
        ? fs.readFileSync(path.join(out, "console-pages.jsonl"), "utf8").trim().split("\n").map(line => JSON.parse(line)) : [],
      navigate: next => { url = next; },
    };
  } finally {
    process.argv = argv;
  }
}

test("an error is recorded with the page that logged it and the digest of its console line", async () => {
  const opened = await tab("https://pay.example/checkout?session=secret#top");
  opened.log(message("error", "Failed to load resource: net::ERR_FAILED", "https://api.example/pageviews?id=1", 0));
  assert.deepStrictEqual(opened.record(), [{
    page: "https://pay.example/checkout",
    sha256: digest("Failed to load resource: net::ERR_FAILED @ https://api.example/pageviews?id=1:0"),
  }]);
});

test("only errors are recorded", async () => {
  const opened = await tab("https://pay.example/");
  opened.log(message("warning", "deprecated"));
  opened.log(message("log", "ready"));
  assert.deepStrictEqual(opened.record(), []);
});

test("what the tab logged before the hook ran is recorded too", async () => {
  const opened = await tab("https://pay.example/", [message("error", "early"), message("info", "hello")]);
  assert.deepStrictEqual(opened.record(), [{ page: "https://pay.example/", sha256: digest("early @ https://cdn.example/app.js:12") }]);
});

test("an error is recorded against the page the tab shows when it is logged", async () => {
  const opened = await tab("https://shop.example/");
  opened.navigate("https://pay.example/checkout");
  opened.log(message("error", "boom"));
  assert.deepStrictEqual(opened.record().map(entry => entry.page), ["https://pay.example/checkout"]);
});

test("a message of several lines is recorded by its first, the one the gate reads", async () => {
  const opened = await tab("https://pay.example/");
  opened.log(message("error", "first line\nsecond line"));
  assert.deepStrictEqual(opened.record().map(entry => entry.sha256), [digest("first line")]);
});

test("an uncaught exception is recorded by the first line of its stack, as the console log writes it", async () => {
  const opened = await tab("https://pay.example/checkout?session=secret");
  const error = new Error("boom\nsecond line");
  error.stack = "Error: boom\nsecond line\n    at https://pay.example/app.js:1:49";
  opened.log(thrown(error));
  assert.deepStrictEqual(opened.record(), [{ page: "https://pay.example/checkout", sha256: digest("Error: boom") }]);
});

test("a rejection with a value that is not an Error is recorded by that value", async () => {
  const opened = await tab("https://pay.example/");
  opened.log(thrown("rejected string"));
  const stackless = new Error("no stack");
  stackless.stack = undefined;
  opened.log(thrown(stackless));
  assert.deepStrictEqual(opened.record().map(entry => entry.sha256), [digest("rejected string"), digest("no stack")]);
});

test("an exception the tab threw before the hook ran is recorded too", async () => {
  const opened = await tab("https://pay.example/", [thrown("early throw")]);
  assert.deepStrictEqual(opened.record(), [{ page: "https://pay.example/", sha256: digest("early throw") }]);
});

test("the output directory is the one playwright-mcp was given, in either spelling", () => {
  assert.strictEqual(hook.outputDir(["node", "cli.js", "--output-dir", "qae-artifacts", "--save-session"]), "qae-artifacts");
  assert.strictEqual(hook.outputDir(["node", "cli.js", "--output-dir=out/qae"]), "out/qae");
  assert.throws(() => hook.outputDir(["node", "cli.js", "--headless"]), /--output-dir/);
});
